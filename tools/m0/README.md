# M0 spike tools

Dev-only tools for the M0 spikes (R1 clock and sync, R2 OBS behaviour, R3 owocr) and for E1.
None of this ships. Run everything from a worktree root with the project interpreter,
`/home/light/Projects/anki_miner_game/.venv/bin/python` (written `python` below).

| Tool | Purpose |
|---|---|
| `tools/nested_display.py` | Isolated nested X11 display (`kwin_wayland --virtual --xwayland`) and the owner guard |
| `tools/obs_transcript_recorder.py` | Websocket proxy that logs every OBS frame with its `t_mono` to JSONL |
| `tools/sync_probe/flasher.py` | Black window that flashes white while it sends a line, like a hooker |
| `tools/sync_probe/analyse.py` | Finds the flashes in a recording; `--raw` against zero events, `--app` against cue starts |
| `tools/m0/obs_scenarios.py` | Scripted OBS scenarios, config-root snapshot and restore, replay-buffer seeding, RTMP sink |

## Isolation comes first

Two incidents on 2026-09-21 shaped the nested display:

- A nested kwin that ran with the owner's `XDG_CONFIG_HOME` and session bus rewrote
  `~/.config/kwinrc` and `~/.config/kwinoutputconfig.json` (it added a `Virtual-0` output), and
  KDE then regenerated `gtkrc`, `gtkrc-2.0`, `Trolltech.conf` and the GTK `settings.ini` files.
- A private bus made with plain `dbus-run-session` auto-activated portals, including a second
  xdg-document-portal, which unmounted the owner's `/run/user/1000/doc`.

Rules:

- Never start `kwin_wayland`, `dbus-run-session`, a `dbus-daemon` or anything that uses portals
  with the owner's environment. Start nested displays only through `tools/nested_display.py`.
- Run `guard` before and after every session. Exit code 3, or any line starting with `CRITICAL`,
  means owner state changed: stop every nested process, report it, and never edit or restore the
  owner's files yourself.
- The orchestrator's watchdog flags any nested kwin whose `XDG_CONFIG_HOME` is outside
  `/home/light/Projects/anki_miner_game/.orchestration/m0/data`, or that uses the owner's session
  bus. The tool refuses such a root before starting anything.

## Nested display

```
python tools/nested_display.py guard --save /home/light/Projects/anki_miner_game/.orchestration/m0/data/r1-clock/guard-before.json
python tools/nested_display.py run --caller r1-clock -- CMD ARGS...
python tools/nested_display.py guard --baseline /home/light/Projects/anki_miner_game/.orchestration/m0/data/r1-clock/guard-before.json
```

`run` starts the display, runs `CMD` inside it and tears everything down when `CMD` exits. For
work spread over several shell calls, keep a display with `serve` (in the background) and start
programs in it with `exec`:

```
python tools/nested_display.py serve --caller r1-clock --env-file /home/light/Projects/anki_miner_game/.orchestration/m0/data/r1-clock/nested.json
python tools/nested_display.py exec --env-file /home/light/Projects/anki_miner_game/.orchestration/m0/data/r1-clock/nested.json -- CMD ARGS...
kill -TERM <serve pid>      # tears down the display and everything started in it
```

What each run gets:

- Private `XDG_CONFIG_HOME`, `XDG_CACHE_HOME`, `XDG_DATA_HOME` and `XDG_STATE_HOME` under
  `.orchestration/m0/data/<caller>/xdg/` (`config`, `cache`, `data`, `state`). kwin writes
  `kwinrc`, `kwinoutputconfig.json` and `kglobalshortcutsrc` there, and its caches (ksycoca,
  mesa, nvidia) under `cache`.
- A private `XDG_RUNTIME_DIR`, a new `/tmp/amg-<random>` (mode 0700) per run, printed at start
  and removed at teardown. It holds the sockets: kwin's, the private bus and Flatpak's bus
  proxy. The proxy goes to `realpath($XDG_RUNTIME_DIR)/.dbus-proxy/session-bus-proxy-XXXXXX`,
  which does not fit in the 107 bytes of `sun_path` under the `.orchestration` prefix, and
  Flatpak's `realpath()` defeats a symlink. The tool refuses a runtime dir that is too long, and
  leaves it in place if anything is mounted inside it.
- A private `dbus-daemon` from `xdg/bus.conf`, which has no `<servicedir>`, no
  `<standard_session_servicedirs/>` and no includes, so nothing can be activated on it; a call
  to a portal fails with `org.freedesktop.DBus.Error.ServiceUnknown`. Its address is the only
  `DBUS_SESSION_BUS_ADDRESS` any nested process sees.
- Environments built from scratch. From the owner: `PATH`, `LANG` and `HOME` only. Children also
  get `DISPLAY` (and `XAUTHORITY` when kwin reports one), `XDG_SESSION_TYPE=x11`,
  `QT_QPA_PLATFORM=xcb`, `GDK_BACKEND=x11`, `SDL_VIDEODRIVER=x11`, and `FLATPAK_USER_DIR`
  pointing at the owner's `~/.local/share/flatpak`. `SESSION_MANAGER`, `AT_SPI_BUS_ADDRESS`,
  `WAYLAND_DISPLAY` and the owner's `XAUTHORITY` never reach a child, and `--setenv` refuses
  them.
- Teardown: SIGTERM to the process group (dbus-daemon, kwin, Xwayland, every `run` or `popen`
  child), a wait, then SIGKILL. Every child carries `AMG_NESTED_RUN=<token>`, so a descendant
  that left the group with `setsid`, or a program started with `exec` from another shell, is
  found in `/proc` and killed too. If the tool itself is killed with SIGKILL, clean up with the
  process group it printed at start, `kill -TERM -- -<pgid>`, then remove the runtime dir it
  printed.
- Logs: `xdg/dbus.log`, `xdg/kwin.log`, `xdg/xwayland-rootful.log`.

### Rootless or rootful Xwayland

- **Rootless (default).** kwin starts Xwayland and is its window manager, so X11 windows have
  `_NET_CLIENT_LIST` entries and OBS's `xcomposite_input` source can list and capture them. Use
  this for OBS, the flasher and E1. A grab of the root window fails here with BadMatch (found
  by R3), so `mss`-style screen grabs do not work.
- **Rootful (`--rootful`).** A second Xwayland with a real `WxH` root window runs as a client of
  the nested kwin, and children get its `DISPLAY`. Root-window grabs work there (owocr, R3), but
  there is no window manager, so OBS window capture has no window list. S2's smoke test covered
  rootless mode only.

## Flatpak OBS inside the display, without the owner's portals

```
python tools/nested_display.py run --caller r1-clock -- \
  flatpak run --die-with-parent --nosocket=wayland --socket=x11 --no-documents-portal \
  --no-a11y-bus --env=QT_QPA_PLATFORM=xcb com.obsproject.Studio
```

`nested_display.flatpak_x11_argv(app_id)` builds the same command. What keeps it off the owner's
session:

- The private `XDG_RUNTIME_DIR` holds no `wayland-0`, `pulse/native`, `pipewire-0` or `doc`
  mount, so Flatpak finds none of the owner's sockets to bind into the sandbox.
- The sandbox's session bus is Flatpak's proxy to the private bus, with its socket in the
  private runtime dir. Portal calls there fail instead of starting portals.
- Flatpak registers the instance in the private runtime dir, so `flatpak ps` in the owner's
  shell does not list this OBS. `obs_scenarios.py` looks for an `obs` process instead.
- `--nosocket=wayland --socket=x11`: the OBS manifest asks for `wayland` with `fallback-x11`;
  this forces X11, so OBS opens in the nested display.
- `--no-documents-portal` and `--no-a11y-bus`: Flatpak does not ask for the document portal or
  the accessibility bus.
- `--die-with-parent`: the sandbox dies with `flatpak run`, which teardown kills.
- `FLATPAK_USER_DIR` lets Flatpak find the `--user` installation although `XDG_DATA_HOME` is
  private. `HOME` stays the owner's, so OBS uses its own config root,
  `~/.var/app/com.obsproject.Studio/config/obs-studio`, which the spikes own.

S2 has not run this: its card forbids launching OBS. From the flags, `flatpak run` needs only
the private bus. Flatpak may still ask the owner's systemd user manager for a transient scope,
which writes no config files. Before R1's first OBS run, check the sandbox:

```
python tools/nested_display.py run --caller r1-clock -- \
  flatpak run --die-with-parent --nosocket=wayland --socket=x11 --no-documents-portal \
  --no-a11y-bus --command=sh com.obsproject.Studio -c \
  'echo "DISPLAY=$DISPLAY WAYLAND_DISPLAY=${WAYLAND_DISPLAY-unset}"; ls "$XDG_RUNTIME_DIR"'
```

Expect a `:N` display, `WAYLAND_DISPLAY=unset`, no `wayland-0` and no `doc`, then an unchanged
`guard --baseline`.

Consequences:

- OBS has no audio devices: there is no PulseAudio socket in the private runtime dir. Do not
  pass `PULSE_SERVER`; WirePlumber would store stream state in the owner's
  `~/.local/state/wireplumber`.
- PipeWire and portal screen capture are not available; they open on the owner's desktop and
  are checked at H5.
- Quit OBS with `pkill -INT -x obs` and wait for it to exit before stopping the display. SIGINT
  closes the main window, whose `closeWindow()` emits the frontend event behind `ExitStarted`
  (obs-studio ba2f32b `frontend/OBSApp.cpp:1871-1891`, `frontend/widgets/OBSBasic.cpp:2034`;
  obs-websocket 1ef34bf `src/eventhandler/EventHandler.cpp:264-265, 459-464`). SIGTERM only
  saves and quits (`frontend/OBSApp.cpp:1895-1914`). An OBS killed by teardown counts as an
  unclean shutdown, and the next launch may stop at the unclean-shutdown prompt
  (`frontend/OBSApp.cpp:1139-1153`), which nobody can click in a headless display.

## Transcript recorder

```
python -m tools.obs_transcript_recorder --upstream ws://127.0.0.1:4455 --port 4456 --out T.jsonl
```

Clients connect to `ws://127.0.0.1:4456` instead of OBS. Every frame is relayed unchanged and
logged with its direction, connection number and the `time.monotonic()` of its receipt; `open`,
`close` (with code and reason) and `upstream_failed` are logged too. Hello's authentication
challenge and salt, Identify's authentication hash and any password field are redacted in the
log only. When OBS is down, the client's handshake fails with HTTP 502. The file is created
fresh; `--append` adds to one.

## Sync probe

`time.monotonic()` is CLOCK_MONOTONIC on Linux, shared by all processes, so the flasher log,
the transcript and the app's own `t_mono` compare directly.

The flasher, inside the display:

```
python tools/nested_display.py exec --env-file .../nested.json -- \
  python -m tools.sync_probe.flasher --log FLASHER.jsonl --port 6677 --count 10 --interval 3 --initial-delay 30
```

It opens a 640x360 black window titled `amg-sync-probe`. At each flash (jittered by up to
`--jitter` seconds, seeded) it turns white for `--frames` frames at `--fps` and sends
`sync probe flash NNN` to every client of its websocket on `127.0.0.1:--port`, the way a hooker
does. Set `--fps` to OBS's frame rate. Flashes before the recording starts are ignored by the
analyser, so a long `--initial-delay` leaves time to start recording.

R1, `--raw` (no app): record with OBS capturing the window (`xcomposite_input`), drive the
recording through the recorder proxy (for example
`python -m tools.m0.obs_scenarios run pause_resume --port 4456 --hold 10 --steps S.jsonl`), then

```
python -m tools.sync_probe.analyse --raw --recording REC.mkv --flasher-log FLASHER.jsonl \
  --obs-transcript T.jsonl --json REPORT.json
```

For each candidate zero event (`StartRecord` sent, its response, `STARTING`, `STARTED`) it prints
the median error (that candidate's `capture_latency_ms`), min, max, spread and the largest
residual around the median. Pauses from the transcript are subtracted.

E1 and releases, `--app`: the app connects to the flasher's port as its hooker source; then

```
python -m tools.sync_probe.analyse --app --recording SESSION.mkv --srt SESSION.srt
```

It passes (exit 0) when every flash has a cue and every cue a flash, each within 150 ms.

The luma threshold defaults to midway between the darkest and the brightest frame, so a window
smaller than the canvas is still found; `--threshold` overrides it.

## Scenarios and the OBS config root

```
python -m tools.m0.obs_scenarios list
python -m tools.m0.obs_scenarios run NAME --port 4456 --steps S.jsonl [--hold 5]
```

Scenarios: `normal`, `pause_resume`, `missed_pause` (the proxied client disconnects, a direct
client pauses, the proxied client reconnects to a paused OBS), `reconnect`, `obs_exit` (asks you
to quit OBS and waits for `ExitStarted`), `prepare_switch_targets` (creates the `amg-probe`
profile and collection, then switches back) and `switch` (there and back, with `--output none`,
`record`, `stream` or `replay_buffer` active). Each step is logged with its start and end
`t_mono`; a refused switch is logged, not fatal. The OBS password comes from
`plugin_config/obs-websocket/config.json` and is never printed.

The spikes use the Flatpak config root directly. With OBS closed:

```
python -m tools.m0.obs_scenarios snapshot --dest DIR       # never overwrites
python -m tools.m0.obs_scenarios restore --snapshot DIR    # only a root named obs-studio
```

## Output-activation recipes

- **Streaming.** Start a local sink, then let the scenario point OBS at it
  (`rtmp_custom`, server `rtmp://127.0.0.1:19350/live`, key `test`):

  ```
  python -m tools.m0.obs_scenarios rtmp-sink --port 19350
  python -m tools.m0.obs_scenarios run switch --output stream --rtmp-port 19350 --port 4456 --steps S.jsonl
  ```

  The sink is `ffmpeg -listen 1 -i rtmp://127.0.0.1:19350/live/test -c copy -f null -` in a loop;
  ffmpeg exits when a publisher disconnects, and the loop starts the next one.
- **Replay buffer.** OBS reads `RecRB` from the profile only at launch (obs-studio ba2f32b
  `frontend/utility/SimpleOutput.cpp:222`, `frontend/utility/AdvancedOutput.cpp:88`; default
  false, `frontend/widgets/OBSBasic.cpp:758, 801`). Quit OBS, seed it, launch OBS, then use
  `--output replay_buffer`:

  ```
  python -m tools.m0.obs_scenarios seed-replay-buffer --profile NAME
  ```

  This sets `RecRB=true` under `[SimpleOutput]` and `[AdvOut]` in
  `basic/profiles/NAME/basic.ini` and leaves every other line alone.
- **Virtual camera.** It cannot be made active on this host: there is no v4l2loopback device.
  Test the refusal path with a recorded response instead: copy a real `GetVirtualCamStatus`
  response (`outputActive: false`) from an R2 transcript, change that one field to `true`, and
  mark the record synthetic (for example `"synthetic": "outputActive edited from false"`), so no
  one mistakes it for OBS output.
