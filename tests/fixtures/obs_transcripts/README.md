# obs-websocket transcripts (R2)

Recorded on 2026-09-21 by the R2 spike from a real OBS Studio **32.2.2** (Flatpak, obs-websocket
**5.7.4**) on Linux, running as an X11 client of the isolated nested display
(`tools/nested_display.py`). Findings: [`docs/m0/obs-behaviour.md`](../../../docs/m0/obs-behaviour.md).
`tests/test_obs_transcript_fixtures.py` pins the set, its redaction and the behaviour each file was
recorded for. T12's `FakeObsServer` replays them.

R2's own driver provisioned the OBS these transcripts talk to, not `obs/provision.py`: its inputs
are `Game capture` and `Game audio` (the app's are `Window Capture (X11)` and `Desktop Audio
Capture`), and its requests come in the driver's order (`arm_disarm.jsonl`'s re-provisioning pass
reads `Game capture` and never asks for a mute). So no transcript replays `ObsProvisioner` request by
request: `tests/obs/test_provision_replay.py` replays `provision.jsonl`'s provisioning
frames against T14's `FakeObs`, and T25 answers the provisioner from a fake OBS and plays only the
recording and switch frames and events of a transcript. A transcript of the app's own provisioning
is E1's to record.

## Format

One record of `tools/obs_transcript_recorder.py` per line (see its module docstring): `open`,
`close` (`by`, `code`, `reason`) and `upstream_failed` records, and frames with `dir`
(`client->obs` or `obs->client`) and `msg` (the obs-websocket v5 message). `t_mono` is the proxy's
`time.monotonic()` at receipt, in seconds.

Each scenario talked to the proxy with obsws-python 1.8.0: a `ReqClient` (usually `conn` 1) and an
`EventClient` with `eventSubscriptions` 2047, all non-high-volume events (usually `conn` 2). A
reconnect opens a new pair (3 and 4). A few scenarios add a third client for one job, named below.
Setup (restoring a snapshot of the OBS config root, launching OBS, readiness polls, quitting) went
straight to OBS and is not in the transcripts.

## Edits made to every file

- Authentication: Hello's `challenge` and `salt` and Identify's `authentication` are `<redacted>`
  (done by the recorder). No password appears anywhere.
- Host paths, longest prefix first: the run's app record folder becomes
  `/home/user/Videos/Anki Miner Game/_incoming` (spec 5 default `output_root`), the user profile's
  record folder becomes `/home/user/Videos`, and any other `/home/light` becomes `/home/user`.
- Nothing else. Window ids (xids), UUIDs, file names and timings are as recorded.

## OBS states the scenarios start from

- **Provisioned.** `provision.jsonl` created the profile and scene collection `Anki Miner Game` on a
  fresh OBS whose only profile and collection were `Untitled`: Simple output mode, NVENC, 1920x1080
  base, 1280x720 output at 30 fps, container `mkv`, split off, auto-remux off, scene `Game` with an
  `xcomposite_input` named `Game capture` on the probe window and a `pulse_output_capture` named
  `Game audio`. OBS had no audio devices in the nested display.
- **Disarmed**: provisioned, OBS launched on `Untitled` / `Untitled`.
- **Armed**: provisioned, OBS launched on `Anki Miner Game` / `Anki Miner Game`.
- The user profile `Untitled` records to its own folder with OBS's defaults. The app's recording
  quality is OBS's default "Same as stream" (`RecQuality=Stream`) unless a row says otherwise.

## Files

| File | Scenario | How it was produced |
|---|---|---|
| `provision.jsonl` | First provisioning (spec 11.3, Linux X11 row) | Fresh OBS on `Untitled`, launched with `--minimize-to-tray`, a probe window open. Four output statuses, profile and collection lists, `CreateProfile`, `SetRecordDirectory`, `SetVideoSettings` (720p, 30/1), `SetProfileParameter` for `SimpleOutput`/`AdvOut` `RecFormat2=mkv`, `AdvOut/RecSplitFile=false`, `Video/AutoRemux=false` with read-backs, `CreateSceneCollection`, `CreateScene Game`, `GetInputKindList`, `CreateInput xcomposite_input` with a placeholder `capture_window`, the window list, `SetInputSettings` to the probe window's item, `CreateInput pulse_output_capture`, `GetSpecialInputs` (all null, so no mute) |
| `settings_apply.jsonl` | Settings without a restart | Straight after `provision.jsonl`, same OBS: record 5 s, stop; switch to `Untitled` and back to `Anki Miner Game`; record 5 s, stop. The first file holds MP4 data under its `.mkv` name, the second is Matroska (ffprobe, in the findings) |
| `arm_disarm.jsonl` | Arm and disarm, no output active | Disarmed OBS. Arm step 1 (four statuses; replay buffer and virtual camera answer 604), lists, `SetCurrentProfile` then `SetCurrentSceneCollection`, the read-only re-provisioning pass, then both switched back to `Untitled` |
| `normal.jsonl` | Normal session | Armed OBS. `StartRecord`, `GetOutputSettings simple_file_output`, `GetOutputList`, a status every 5 s for 15 s, `StopRecord`, then `GetRecordStatus` every 10 ms until it reports inactive |
| `pause_noop.jsonl` | Pause on the default profile | Armed OBS (`RecQuality=Stream`). `PauseRecord` 3 s in, `ResumeRecord` 5 s later, stop |
| `pause_resume.jsonl` | Pause and resume | Armed OBS with `SimpleOutput/RecQuality=Small` seeded in `basic.ini` before launch (a separate recording encoder). Pause 4 s in for 4 s, resume, stop 4 s later |
| `missed_pause.jsonl` | Missed pause event | As `pause_resume.jsonl`. 3 s into the recording both proxied connections close; 1 s later a direct client (not in the transcript) sends `PauseRecord`; 3 s after that a new pair connects, reads the status (paused), resumes and stops |
| `reconnect.jsonl` | Reconnect mid-session | Armed OBS. Both connections close 3 s into the recording; a new pair connects 5 s later: `GetVersion`, `GetRecordStatus`, `GetOutputList`, `GetOutputSettings`, stop 3 s later |
| `split.jsonl` | Automatic file splitting on | Armed OBS with `[Output] Mode=Advanced`, `AdvOut/RecType=Standard`, `RecSplitFile=true`, `RecSplitFileType=Manual` seeded before launch. `SplitRecordFile` 4 s in, `RecordFileChanged`, stop 4 s later |
| `split_off_runtime.jsonl` | Split turned off by a request | Seeded as `split.jsonl`. `SetProfileParameter AdvOut/RecSplitFile=false` while OBS runs, then record: `SplitRecordFile` answers 702 and no split happens |
| `obs_exit.jsonl` | OBS exits while recording | Armed OBS with `[General] ConfirmOnExit=false` seeded in `user.ini`. SIGINT to `obs` 5 s into the recording (closes the main window like the user would); two reconnect attempts after it is gone |
| `obs_sigterm.jsonl` | SIGTERM while recording | Armed OBS, `ConfirmOnExit` left at its default. SIGTERM 5 s into the recording; OBS stayed up; the pair closes about 68 s later and a second pair connects and closes at once. OBS was then ended with SIGINT, which crashed it (findings) |
| `obs_killed.jsonl` | OBS killed while recording | Armed OBS. SIGKILL 5 s into the recording; two reconnect attempts. The relaunch that followed stopped at OBS's crash dialog (findings) |
| `switch_recording.jsonl` | Switch with a recording active | Disarmed OBS, `Untitled` set to `RecFormat2=mkv`. `StartRecord` on `Untitled`, arm step 1 statuses, both switches to the app, statuses, 4 s, statuses, both switches back, statuses, stop |
| `switch_streaming.jsonl` | Switch with a stream active | As `switch_recording.jsonl` with `SetStreamServiceSettings` (`rtmp_custom` to a local `ffmpeg -listen 1` sink, `tools/m0/obs_scenarios.py rtmp-sink`) and `StartStream` instead of recording. A colour source was added to `Untitled`'s scene first (direct client), as for the replay buffer |
| `switch_replay_buffer.jsonl` | Switch with the replay buffer active | As `switch_streaming.jsonl` with `RecRB=true` seeded in `Untitled`'s `basic.ini` before launch and `StartReplayBuffer`. The colour source is needed here: from a scene without a video source, `StartReplayBuffer` answered 100 and OBS stopped at a modal "no sources" question (a first attempt, not kept) |
| `switch_refused.jsonl` | Refused and no-op switches | Armed OBS. `SetCurrentProfile` and `SetCurrentSceneCollection` to unknown names (600), then to the current ones (100, no events), then `CreateProfile`, `CreateSceneCollection` and `CreateScene` with existing names (601) |
| `switch_not_ready.jsonl` | Requests during a collection change | Disarmed OBS. Profile to the app; then a third client (`conn` 3) sends `GetRecordStatus` in a loop while the collection switches to the app and back; profile back |
| `switch_restart_prompt.jsonl` | OBS's restart question | Disarmed OBS with `Untitled` at `[Audio] SampleRate=44100`; the app profile carries OBS's 48000. `SetCurrentProfile` to the app from a third client (`conn` 3) with a 20 s timeout: the events arrive, the answer never does (a modal "Restart" window). OBS was then ended with SIGTERM |
| `switch_restart_prompt_matched.jsonl` | Same rates, no question | As `switch_restart_prompt.jsonl` with the app profile also set to 44100 before launch. Four switches, each from its own client (`conn` 3 to 6), all answered |
| `start_failed_missing_dir.jsonl` | `StartRecord` into a missing folder | Armed OBS whose `_incoming` folder was removed after launch. `StartRecord` answers 100, no event follows, OBS shows a modal "Bad File Path" |
| `start_failed_unwritable.jsonl` | `StartRecord` into a read-only folder | Armed OBS, `_incoming` set to mode 0555 after launch. `STARTING` and then nothing, OBS shows a modal "Failed to start recording" |
| `window_retitle.jsonl` | Captured window retitled, then closed (spec 12) | Armed OBS with the probe window open, OBS forced to Mesa EGL (`__EGL_VENDOR_LIBRARY_FILENAMES`) in an attempt to get a visible capture. Window list and settings, record, the probe retitles itself to `amg-probe-window - level 2 - 59 fps`, list and settings again, the probe closes, list again, stop. `GetSourceScreenshot` fails with 702 throughout and the file is black: `xcomposite_input` renders nothing on this host with either GL (R1 side finding 1) |
| `synthetic-arm-virtualcam-active.jsonl` | Arm refused, virtual camera active | **Synthetic.** `arm_disarm.jsonl` cut after arm step 1's four status requests, followed by that file's two `close` records. The `GetVirtualCamStatus` response, a real 604 "VirtualCam is not available." (no v4l2loopback here), was edited to success with `outputActive: true` and carries a `synthetic` field saying so |

## Reproducing

The driver that ran these scenarios is not part of the repository; it is kept with the raw runs
(recordings, OBS logs, ffprobe output, per-step logs) under
`/home/light/Projects/anki_miner_game/.orchestration/m0/data/r2-obs-behaviour/`
(`driver/r2_driver.py`, `driver/campaign.py`, `driver/build_fixtures.py`).
