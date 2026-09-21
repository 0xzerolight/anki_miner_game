# M0 R1: record clock and sync spike

Run 2026-09-21 on the Linux host (KDE Plasma on Wayland, RTX 5070 Ti Laptop GPU with driver
595.91.07, 16 cores / 32 threads). OBS Studio **32.2.2** Flatpak (obs-websocket 5.7.4), OpenGL on
the NVIDIA GPU. Source cites are against the clones at obs-studio `ba2f32b` and obs-websocket
`1ef34bf`, the same ones S1 used.

Spec 20 rows "Sync probe and clock", "Pause" and "Disk rate"; spec 7; spec 18.3.

| Item | Result |
|---|---|
| Zero event | **`STARTED`** (`RecordStateChanged` with `OBS_WEBSOCKET_OUTPUT_STARTED`, `t_mono` at receipt) |
| `capture_latency_ms` | **10** (median of the 222 flashes in the four non-overload configurations: 9.5 ms) |
| Spread across encoders | **50 ms** between the encoder medians with `STARTED` (-15 to +35). Under 100 ms: one constant ships, no calibration step. With the `StartRecord` response or `STARTING` as the zero it would be about 183 ms |
| Spread per encoder | 46 to 54 ms within each configuration (all flashes, three sessions each); 128 ms under encoder overload |
| Pause | On OBS's default profile `PauseRecord` does nothing (success reply, no event, recording continues). With a separate recording encoder, offsets after resume stay within 10 ms (median) of those before |
| 150 ms bound | **Pass** for every flash of every run, after pause included. Largest residual against 10 ms: 50 ms without overload, 133 ms under encoder overload |
| Disk rate, OBS defaults | **2.78 GB/h at 1080p30 and 2.78 GB/h at 720p30** (NVENC or x264, CBR 6000 kb/s video + AAC 160 kb/s). Resolution does not change it |

Two findings need spec changes beyond the constants (sections "OutputDurationClock" and "Side
findings"): the fallback clock trails by 0.46 to 5.6 s depending on the encoder, and one window-list
request aborts OBS on Linux.

All numbers are Linux. The Windows zero event and latency stay provisional until H5 (D2).

## Setup

- **Display.** `tools/nested_display.py serve --caller r1-clock-sync --rootful --width 1920
  --height 1080`: a private `kwin_wayland --virtual --xwayland` with a rootful Xwayland inside,
  private XDG dirs under `.orchestration/m0/data/r1-clock-sync/xdg`, a private runtime dir under
  `/tmp/amg-*` and a private bus without service activation. OBS and the flasher ran as X11
  clients of the rootful server.
- **OBS.** `flatpak run --die-with-parent --nosocket=wayland --socket=x11 --no-documents-portal
  --no-a11y-bus --env=QT_QPA_PLATFORM=xcb com.obsproject.Studio`. The config root
  (`~/.var/app/com.obsproject.Studio/config/obs-studio`) did not exist before R1. It was seeded with
  `user.ini` `[General] FirstRun=true` (skips the auto-configuration wizard,
  `frontend/widgets/OBSBasic.cpp:1287-1296`) and a websocket `config.json` (server enabled, port
  4455, authentication on, generated password, `first_load` false). After one clean launch it was
  snapshotted, restored before every campaign, and restored again at the end. The sandbox check
  from `tools/m0/README.md` showed `DISPLAY=:1`, `WAYLAND_DISPLAY` unset and no `wayland-0`, `doc` or
  `pulse` in the sandbox runtime dir; the NVIDIA GL extension (`GL.nvidia-595-91-07`, already
  installed) was mounted, so no Flatpak filter line was added.
- **Capture.** An XSHM screen capture (`xshm_input_v2`, screen 0, cursor off) of the rootful
  Xwayland, cropped with `cut_*` to the flasher window (640x360 at 0,0, read with `xwininfo` per
  session), on a 1920x1080 canvas, output 1920x1080 at 30/1 fps. `xcomposite_input` could not be
  used on this rig (side finding 1).
- **Flasher.** `tools/sync_probe/flasher.py`, one per session: 20 flashes of 3 white frames, the
  first at 8 s, then every 2.5 s plus up to 0.5 s of seeded jitter. It logs `time.monotonic()`
  just before its synchronous repaint, so X server and capture delay are inside the measured error,
  as they would be for a game.
- **Driving OBS.** Every request went through `tools/obs_transcript_recorder.py` (a logging proxy).
  Per session: `StartRecord` 4 s before the first flash; `PauseRecord` midway between flashes 7
  and 8 (0-based) and `ResumeRecord` midway between flashes 10 and 11, so flashes 8 to 10 fall in
  the pause; `StopRecord` 2.5 s after the last flash. Three sessions per OBS launch; OBS was quit
  with SIGINT after each campaign.
- **Analysis.** `tools/sync_probe/analyse.py --raw`: for each candidate zero event, the error of a
  flash is its first white frame in the file (ms from the container start, `format.start_time`,
  which is -21 ms in every probe recording because the audio starts first) minus
  `(t_flash - zero - paused_before) * 1000`, with pauses taken from the `PAUSED` and `RESUMED`
  event times. A positive error means the flash shows up later in the file than the clock
  predicts; `capture_latency_ms` is the constant that best centres it.
- **Encoder overload.** Advanced output with x264 `veryslow` and a looping 1080p30 source of
  `testsrc2` plus temporal noise (with pink-noise audio) under the flasher window. OBS logged 13.3,
  14.4 and 55.4 % of frames skipped due to encoding lag in the three sessions. A one-session probe
  with `slower` gave 1.6 %, one with `placebo` 91.3 %.

Throwaway driver and aggregator (not in the repo): `.orchestration/m0/data/r1-clock-sync/driver/`
(`r1_driver.py`, `aggregate.py`, `run_all.sh`). Raw data, recordings, OBS logs and transcripts:
`.orchestration/m0/data/r1-clock-sync/runs/<run>/`; the combined numbers:
`.orchestration/m0/data/r1-clock-sync/agg-all.json`.

## Zero event and capture latency

Error in ms with `STARTED` as the zero, 1080p30, three sessions of 20 flashes per configuration.
The encoder settings are those OBS logged for the recording encoder.

| Configuration | Encoder as logged | Flashes recorded / found | Median | Min | Max | Spread | Session medians |
|---|---|---|---|---|---|---|---|
| OBS defaults: Simple, recording shares the stream encoder | NVENC H.264 p5, CBR 6000, lookahead on (8 frames), 2 B-frames | 60 / 60 | -15 | -37 | +13 | 50 | -24.5, -2, -17 |
| Simple, x264 (OBS's choice without NVENC) | x264 veryfast, CBR 6000 | 60 / 60 | +23.5 | 0 | +48 | 48 | +11.5, +34.5, +24 |
| Advanced, x264 with lookahead 60 | x264 veryfast, CBR 6000, custom `rc-lookahead = 60` | 51 / 51 | +35 | +6 | +60 | 54 | +30, +41, +39 |
| Advanced, NVENC defaults | NVENC H.264 p5, CBR 6000, lookahead on (8 frames), 2 B-frames | 51 / 51 | -6 | -29 | +17 | 46 | -5, -10, -2 |
| Encoder overload | x264 veryslow, CBR 6000, 13-55 % skipped | 51 / 44 | +38.5 | +15 | +143 | 128 | +60, +35, +33 |
| Mild overload (one session) | x264 slower, CBR 6000, 1.6 % skipped | 9 / 9 | -1 | -9 | +22 | 31 | -1 |

"Recorded" excludes the three flashes inside each pause (Advanced runs only; on the Simple runs the
pause did nothing, see below). No flash went unmatched except under overload.

The other candidates, same flashes, medians in ms:

| Configuration | `StartRecord` sent | its response | `STARTING` | `STARTED` | `STARTING` to `STARTED` |
|---|---|---|---|---|---|
| OBS defaults (NVENC) | -156 | -155.5 | -155.5 | -15 | 134-184 ms |
| Simple, x264 | +15 | +15.5 | +15.5 | +23.5 | 7-15 ms |
| Advanced, x264 lookahead 60 | +27 | +27 | +27 | +35 | 7.7-8.4 ms |
| Advanced, NVENC | -150 | -150 | -149 | -6 | 132-176 ms |
| Encoder overload | +28 | +28 | +28.5 | +38.5 | 8.5-13 ms |

- The request, its response and `STARTING` arrive within 1 ms of each other (response 0.1-0.5 ms,
  `STARTING` 0.2-0.9 ms after the request), as S1 read from source (source findings section 6).
  `STARTED` follows once the encoder has started: 7-15 ms later for x264, 132-184 ms later for
  NVENC. That gap moves the earlier candidates by 180 ms between encoders; `STARTED` removes it.
- With `STARTED` the two encoders still differ by about one frame (NVENC medians -15 and -6, x264
  +23.5 and +35). The cause was not investigated; it is under the 100 ms criterion either way.
- Lookahead does not move the clock: x264 at `rc-lookahead = 60` (2 s at 30 fps) sits 11.5 ms
  from the default x264 median, and NVENC's own 8-frame lookahead is on in both NVENC rows. The
  file's timestamps do not depend on when the encoder releases a frame.
- One session's median differs from another's by up to 23 ms with the same encoder (a frame
  period is 33 ms).
- **Recommendation for spec 7:** zero = `STARTED`, `capture_latency_ms = 10`. Against 10 the
  largest residual is 50 ms without overload (133 ms under overload). The current default (0) would
  also pass (60 ms and 143 ms), with less margin under overload.

## Encoder overload

When the encoder falls behind, OBS keeps its frame timeline: the video thread sends the last frame
it accepted again in place of new ones (`libobs/media-io/video-io.c:521-523`, and the timestamp
advances one frame per send at `video-io.c:168`). So the recording's clock does not drift; what
changes is that a flash rendered while the cache is full is lost, and the first white frame in the
file is a later one.

- At 13-14 % skipped, errors run from +20 to +143 ms (two flashes above 100 ms).
- At 55 % skipped, 7 of 17 recorded flashes did not appear in the file at all.
- At 91 % skipped (`placebo` probe), 1 of 9 recorded flashes appeared, at +4 ms.

For the product this means a line can reach the subtitle before its picture reaches the video
under heavy load. It stays inside the 150 ms bound up to at least 14 % skipped frames; nothing in
the clock can correct it.

## Pause

**OBS's default profile cannot pause** (runtime confirmation of S1 finding 3). With Simple output
and recording quality "Same as stream" (both defaults), `PauseRecord` answered success (code 100),
no `PAUSED` event came within 3 s, `GetRecordStatus` kept `outputPaused: false`, and
`ResumeRecord` answered 503 `OutputNotPaused`. The recording ran on through the "pause", and the
three flashes inside it were recorded at their normal offsets: errors before and after were the
same (medians -15.5 and -15 ms for NVENC, +28 and +21.5 ms for x264). For the app, a `PauseRecord`
without a `PAUSED` event must not be treated as a pause.

**With a separate recording encoder** (Advanced output, `RecEncoder` set), `PAUSED` and `RESUMED`
arrived and the recording skipped the paused span. Subtracting `RESUMED - PAUSED` (event receipt
times) as spec 7 does:

| Configuration | Before the pause: median (min..max) | After resume: median (min..max) |
|---|---|---|
| Advanced, x264 lookahead 60 | +32.5 (+6..+49) | +41 (+11..+60) |
| Advanced, NVENC | -5 (-19..+17) | -9 (-29..+14) |
| Encoder overload | +37 (+20..+107) | +39 (+15..+143) |

The frame-aligned pause edges (source findings section 6) move offsets by under 10 ms at the median.
Pause passes the 150 ms bound.

## OutputDurationClock (spec 7 fallback)

`GetRecordStatus.outputDuration` was sampled after `STARTED`, around each pause edge and before
`StopRecord`. The lag below is the event-clock offset at the midpoint of the request round trip
(zero `STARTED`, pauses subtracted) minus `outputDuration`:

| Configuration | Lag (ms), all samples |
|---|---|
| OBS defaults (NVENC) | 552-584 |
| Advanced, NVENC | 457-576 |
| Simple, x264 veryfast | 1605-1637 |
| Advanced, x264 lookahead 60 | 3217-3284 |
| Encoder overload (x264 veryslow) | 3411-5577 |

`outputDuration` counts delivered frames (source findings section 6), so it trails by the
encoder's whole latency, which
depends on the encoder, its lookahead and its thread count, and grows under overload. An
`OutputDurationClock` anchored on it as spec 7 describes would start cues 0.46 to 3.3 s early with
a healthy encoder and up to 5.6 s early under overload. Within one session of the Simple runs
(three samples, no effective pause) the lag moved by at most 27 ms; in the Advanced runs it moved
by 37 to 110 ms between the sample just after resume and the one before `StopRecord`.

**Proposed amendment (spec 7, 6.3):** the fallback adds the lag measured in the same session, taken
from the `clock.drift_samples` recorded while the event history was complete (so the drift samples
become an input for the fallback); when a session has no such sample, the fallback cannot be
trusted and the session is flagged. The M0 gate decides the flag name and whether reconcile should
rather keep the event clock with a persisted `zero_mono` (`time.monotonic()` is one clock for all
processes since Python 3.5, so it survives an app restart, not a reboot).

## Disk rate

Two minutes per resolution, OBS defaults (Simple output, recording shares the stream encoder,
`VBitrate` 6000 and `ABitrate` 160 by default, `frontend/widgets/OBSBasic.cpp:752-757`). OBS picked
NVENC on this host (`OBSBasic.cpp:884-889`); the x264 row sets `StreamEncoder=x264`, which is the
default on a machine without NVENC. "Busy" is the 1080p30 noise source; "static" is an empty scene.

| Encoder, scene | 1080p30 | 720p30 |
|---|---|---|
| NVENC (OBS default here), busy | 2.776 GB/h | 2.779 GB/h |
| x264 veryfast, busy | 2.770 GB/h | 2.775 GB/h |
| NVENC, static (1 min each) | 2.771 GB/h | 2.771 GB/h |

Per stream: video 5992-6011 kb/s, audio 160 kb/s. GB = 10^9 bytes; rate = file size / container
duration. Both encoders ran CBR and held the bitrate on the empty scene as well (NVENC measured;
x264 turns on filler for CBR, `plugins/obs-x264/obs-x264.c:467-472`), so the rate depends on the
bitrate only.

**Proposed amendment (spec 21, user guide, free-space warning):** 2.8 GB per hour at 1080p30 and at
720p30 with OBS's default encoder settings. The risk row's "1.5-3 GB per hour" becomes 2.8, and
"720p is one setting away" does not reduce disk use while the encoder settings stay OBS's.

## Side findings

1. **`xcomposite_input` records black on this rig.** OBS 32.2.2 on EGL/X11 with the NVIDIA driver
   logged `Cannot create EGLImage: One or more argument values are invalid.` every 2 s and recorded
   a flat black frame (mean luma 16.94) for the flasher window in the nested rootless Xwayland
   (run `campaign-simple-default-trial2-173143`). EGL is OBS's only X11 backend (`libobs-opengl`
   has `gl-x11-egl.c`, no GLX file), so there is no GLX fallback to try. The rig therefore uses XSHM
   of a rootful Xwayland. E1's card asks for `capture.kind=xcomposite` with this OBS on this host;
   it will need the same XSHM workaround or a Mesa GL for OBS. Whether real NVIDIA X11 desktops hit
   this is for H5.
2. **Listing windows of an `xcomposite_input` with an empty `capture_window` aborts OBS.**
   `GetInputPropertiesListPropertyItems(inputName, propertyName="capture_window")` on a freshly
   created input killed OBS 32.2.2: `terminate called after throwing an instance of
   'std::logic_error' what(): basic_string: construction from null is not valid` (run
   `campaign-simple-default-trial-173018`, `obs-stdout.log`). With no saved window, item 0 ("Select
   a window") has a NULL value (`plugins/linux-capture/xcomposite-input.c:766-768`); obs-websocket
   puts it into JSON as a string (`src/utils/Obs_ArrayHelper.cpp:277`) inside a thread-pool task
   (`src/websocketserver/WebSocketServer.cpp:352`), and the exception ends the process. Creating the
   input with any non-empty placeholder value avoids it (every later run). T14 (window list) and the
   game profile dialog must never list windows of an input whose `capture_window` is empty.
3. **Quitting OBS while a recording is still stopping hangs a headless OBS.** SIGINT while an
   overloaded x264 was still draining after `StopRecord` opened the "Active Outputs" question
   (`frontend/widgets/OBSBasic.cpp:1960`), which nobody can answer in the nested display; OBS kept
   running. SIGTERM then saved and quit. Anything that stops OBS (R2, E1, the app's restart after
   provisioning) should wait for `outputActive: false` first.
4. **OBS's default encoder here is NVENC with lookahead.** Simple output picks NVENC when it is
   available (`frontend/widgets/OBSBasic.cpp:884-889`), and NVENC's lookahead default follows the
   GPU's capability (`plugins/obs-nvenc/nvenc-properties.c:56`), on here (8 frames).

## Limits

- One host, one OBS build, Linux only. The capture path is XSHM, not `game_capture` or PipeWire;
  Windows (`game_capture`) and PipeWire capture are H5 checks.
- 30 fps only; at 60 fps the frame-phase part of the spread halves.
- OBS had no audio devices in the nested display (no PulseAudio socket). The audio track came from
  the busy media source where there was one, and was silent otherwise; the AAC encoder still wrote
  160 kb/s.
- The raw probe measures the clock, not the app: it uses the transcript's event times where the app
  will use its own receipt times. E1 repeats the probe with `--app` against the app's `.srt`.
- Windows at H5: besides the zero event and latency with `game_capture`, check
  `time.get_clock_info("monotonic").resolution` on the shipped Python. CPython on Windows is
  reported to have used a 15.6 ms tick for `time.monotonic()` before 3.13 (not verified here);
  at that resolution every line's `t_mono` would carry up to 16 ms of extra error.

## Owner environment

Guard baseline before the first nested run (`guard-before.json`, `owner-mtimes-before.txt`) and
after the last (`guard-after.txt`, `owner-mtimes-after.txt`), plus a guard check after every
campaign: `kwinrc`, `kwinoutputconfig.json`, `kglobalshortcutsrc`, `gtkrc`, `Trolltech.conf`
and the GTK and xsettingsd files unchanged; `/run/user/1000/doc` stayed `fuse.portal`. No nested
kwin, Xwayland, dbus-daemon, OBS or `/tmp/amg-*` dir was left. `~/.config/owocr_config.ini` does
not exist. The OBS config root was restored to the post-first-launch snapshot at the end
(`.orchestration/m0/data/r1-clock-sync/obs-baseline`).
