# M0 R3: owocr spike

Run 2026-09-21 on the Linux host (KDE Plasma, Wayland). owocr **1.26.8** (newest on PyPI that day,
upload 2026-03-28), engine `meikiocr`, source cites against the 1.26.8 clone at `3b9706b1`
(`owocr/run.py`, `owocr/config.py`, `owocr/ocr.py`, `owocr/__main__.py`, `pyproject.toml`).

Spec 18.3 row "owocr": *the coordinate log line parses; the process tree dies through the uv shim
on both platforms; exact install floor.*

| Item | Result |
|---|---|
| Coordinate log line | `Selected coordinates:` captured from real runs (one and two rectangles). `Selected window coordinates:` cannot be produced on Linux; fixtures are synthetic and labelled |
| Process tree dies | Linux: yes, `os.killpg` on a process group started with `start_new_session=True` removes every process, including the picker's grandchildren. Killing only the parent leaves two orphans. Windows: H5 |
| Install floor | Python >= 3.11. `uv tool install "owocr[meikiocr]==1.26.8"` **fails on Linux** without cairo (and GObject introspection) dev packages; succeeds with `pygobject` overridden out, which only works for X11 capture. Windows: H5 |
| OCR through the websocket | Three lines from a synthetic window. A changed line reached the websocket 54-65 ms after the change (two changes); meikiocr's own recognition took 49-62 ms per frame |
| `~/.config/owocr_config.ini` | owocr **creates it** on first run (downloaded from GitHub) and reads it on every later run. Redirecting `HOME` contained it; the real file did not exist before or after the spike |
| Owner desktop | **Touched.** No window opened on it, but the nested kwin shared the owner's config dir and session D-Bus and rewrote eight owner config files (section "Owner-environment incident") |

Five findings need spec changes (section "Proposed spec amendments"); one of them (Linux Wayland
install) may need an owner decision.

## Setup

- Install: real uv 0.11.22, `UV_TOOL_DIR` / `UV_TOOL_BIN_DIR` under
  `.orchestration/m0/uv-tools/` (gitignored), `--python 3.12`. uv resolved its managed CPython
  3.12.13 from `~/.local/share/uv/python` (outside `UV_TOOL_DIR`) and used the default uv cache.
- Display: a private `kwin_wayland --virtual --xwayland` (own socket, 1280x720 virtual output),
  and inside it a **rootful** `Xwayland :N -geometry 1280x720` started as a client of the nested
  kwin. Every probe process ran as an X11 client of that rootful server with `WAYLAND_DISPLAY`
  unset and `XDG_SESSION_TYPE=x11`. No window opened on the owner desktop, but the nested kwin was
  not isolated from the owner session (section "Owner-environment incident").
  - Why rootful: the first R3 instance reported that mss's `GetImage` on the root window of kwin's
    rootless Xwayland failed with `BadMatch` (X error 8, major opcode 73), which owocr turns into
    `The window was closed or an error occurred` and an exit (`run.py:2544`). That probe's output
    (`.orchestration/m0/r3/msstest.sh`) was not kept, so this is **unverified**; the only trace is
    the header of `r3/session.sh`. S2 should re-check it before copying the rootful step into
    `tools/nested_display.py`.
  - kwin also started an fcitx instance, which tried to take its name on the owner's session bus,
    failed (`Is there another fcitx already running?`, `out-*/kwin.log`) and unloaded.
- owocr environment: `HOME`, `XDG_CACHE_HOME` and `XDG_CONFIG_HOME` pointed at a scratch dir.
- Synthetic window: a frameless PyQt6 `QLabel` (X11 bypass-WM hint) at screen `100,100` size
  800x160, Noto Sans CJK JP 48 pt, black on white, showing three sentences 15 s apart and printing
  `time.monotonic()` at each change.
- Harness (not committed; gitignored scratch): `.orchestration/m0/r3/{nest.sh,session.sh,driver.py,text_window.py}`,
  raw outputs in `.orchestration/m0/r3/out-*`. Two R3 instances ran at once and wrote the same
  `out-*` dirs; the kept set is the 14:59:03-15:00:28 one, and the captured fixtures match it byte
  for byte after redaction. A figure with no file behind it is marked as not kept.

Command used (spec 14 Linux form with explicit rectangles):

```
owocr -r screencapture -w websocket -wp <free port> -t False -l ja -e meikiocr -sa 100,100,900,260
```

## Owner-environment incident

`nest.sh` started `kwin_wayland --virtual --xwayland` with the owner's environment minus
`WAYLAND_DISPLAY` and `DISPLAY`, so the nested kwin kept the owner's `HOME`, `XDG_CONFIG_HOME`,
`XDG_RUNTIME_DIR`, `DBUS_SESSION_BUS_ADDRESS` and `SESSION_MANAGER`. It wrote the owner's config
(`stat` through `.orchestration/m0/r3/owner-check.sh`, after the spike):

| File under `~/.config/` | mtime 2026-09-21 | Nested run at that time |
|---|---|---|
| `kwinrc` | 14:57:10 | first run (its output dir did not survive) |
| `xsettingsd/xsettingsd.conf`, `gtk-3.0/settings.ini`, `gtk-4.0/settings.ini` | 14:57:08 | first run |
| `kwinoutputconfig.json` | 15:00:28 | variants run (15:00:19-28); now holds a `Virtual-0` output |
| `gtkrc`, `gtkrc-2.0`, `Trolltech.conf` | 15:00:20 | variants run |

- The second R3 instance also saw `kwinoutputconfig.json` at 14:59:03, 14:59:33 and 14:59:39, the
  ocr and picker runs (addendum of `.orchestration/reviews/r3-owocr-impl-report.md`).
- Unchanged: `kglobalshortcutsrc` (2026-09-14), `findmnt /run/user/1000/doc` (`fuse.portal`), and
  `~/.config/owocr_config.ini` (absent).
- The session bus was the owner's. Every `out-*/kwin.log` shows `Failed to register service
  org.kde.kglobalaccel`, `Failed to register with host portal` and fcitx's `Unable to request dbus
  name`: names already held on the owner's bus.
- R3 did not restore the files; that is the orchestrator's call.

Isolation S2's `tools/nested_display.py` needs. R3 wrote `.orchestration/m0/r3/iso-nest.sh` along
these lines but never ran it, so none of this is verified:

- Start kwin from `env -i` with an explicit environment, so no owner `DBUS_SESSION_BUS_ADDRESS`,
  `SESSION_MANAGER`, `DISPLAY`, `WAYLAND_DISPLAY` or `XDG_SESSION_*` is inherited.
- Private `HOME`, `XDG_CONFIG_HOME`, `XDG_CACHE_HOME`, `XDG_DATA_HOME`, `XDG_STATE_HOME` and
  `XDG_RUNTIME_DIR` (mode 0700).
- A private `dbus-daemon` session bus from a config with no `<servicedir>`, so nothing is
  bus-activated; stop it after the run.
- Fingerprint the files above and `findmnt /run/user/<uid>/doc` before and after every run, and fail
  the run on any change.

## Coordinate log lines

Format, from source: loguru to `sys.stderr` with format `{time:HH:mm:ss} | {message}`
(`run.py:3097,3100`), so a line reads `HH:MM:SS | Selected coordinates: <rects>`. `<rects>` is
`x1,y1,x2,y2`, several rectangles joined by `_` (`run.py:1954,2477`). Local time, no date.

| Line | Printed when | Source |
|---|---|---|
| `Selected coordinates: <rects>` | explicit `-sa <rects>` at start-up, and after the screen picker (`-sa ""`) returns a selection | `run.py:1956`, `run.py:2480` |
| `Selected window coordinates: <rects>` | explicit `-swa <rects>` after a window match, and after the window picker (`-swa ""`) returns a selection | `run.py:2035`, `run.py:2517` |
| `Selection is empty, selecting whole screen` / `... whole window` | picker returned no rectangle | `run.py:2483`, `run.py:2521` |
| `Picker window was closed or an error occurred` | screen picker closed; fatal when `-sa ""` (exit code 1) | `run.py:2448` |
| `Picker window was closed or an error occurred, selecting whole window` | window picker closed; **not fatal**, owocr keeps running | `run.py:2502` |

Observed in the captured logs:

- The log goes to stderr (`__main__.py:7-10` points fd 2 at `/dev/null` for native noise and gives
  `sys.stderr` the original descriptor). Piped, it carries no ANSI colour codes.
- Rectangles are echoed verbatim when valid. A rectangle outside every monitor is dropped
  (`run.py:2041-2085`); when none remain owocr logs `Invalid coordinate set(s) in
  screen_capture_area` and exits with code 1.
- A window name in `-sa` on Linux X11 exits with code 1: `Window capture is only currently
  supported on Windows, macOS and Linux + Wayland` (`run.py:2017`). On Linux Wayland a window name falls
  through to the screen picker instead (`run.py:1923-1924`). So on Linux `-swa`, and with it
  `Selected window coordinates:`, is never reached; the window line is Windows-only.
- After the picker line owocr **keeps running** and starts OCR. "Select OCR area" must kill it once
  the line is read (or on a picker-closed line, or exit).

Fixtures in `tests/fixtures/owocr/` (pinned by `tests/test_owocr_fixtures.py`):

| File | Kind | Coordinate line |
|---|---|---|
| `linux-x11-explicit-rect.log` | captured | `Selected coordinates: 100,100,900,260` |
| `linux-x11-multi-rect.log` | captured | `Selected coordinates: 100,100,500,260_500,100,900,260` |
| `linux-x11-whole-screen.log` | captured (`-sa screen_1`) | none (`Selected whole screen`) |
| `linux-x11-picker-killed.log` | captured (`-sa ""`, killed at the picker) | none |
| `linux-x11-off-screen-rect.log` | captured, exit 1 | none |
| `linux-x11-window-name.log` | captured, exit 1 | none |
| `synthetic-screen-picker.log` | synthetic, from `run.py:3159,2436,2480` | `Selected coordinates: 412,610,1508,1002` |
| `synthetic-window-picker.log` | synthetic, from `run.py:3159,2015,2494,2517` | `Selected window coordinates: 0,540,1280,720` |
| `synthetic-window-picker-multi-rect.log` | synthetic, from `run.py:2515-2517` | two rectangles joined by `_` |

Captured logs are verbatim except three path prefixes: `<owocr-home>` (scratch `HOME`),
`<tool-env>` (the uv tool venv) and `<uv-python>` (uv's managed CPython). The synthetic files carry
a `# SYNTHETIC` first line; a real picker round trip is H4 (and Windows H5).

The captured explicit-rect log also shows a traceback: `user_input_thread_run` calls
`termios.tcgetattr` on stdin (`run.py:2970`), which raises when stdin is not a TTY. Only that
thread dies; OCR carries on. The supervisor must not treat a traceback in the log as a failure.

## Process tree

owocr's process group started with `start_new_session=True` (evidence in
`.orchestration/m0/r3/out-*/*-events.jsonl`; the user `systemd` is pid 2991, `systemd --user`):

| Run | Processes in the group | Kill | Result |
|---|---|---|---|
| Explicit rectangles (OCR running) | 1: the tool venv's `python .../bin/owocr` | `os.killpg(pgid, SIGTERM)` | all gone in 16 ms (one run), return code -15 |
| Picker (`-sa ""`) | 3: owocr, `multiprocessing.resource_tracker`, the `spawn_main` picker child (`screen_coordinate_picker.py:484`) | `os.kill(pid, SIGKILL)` on the parent only | **2 orphans** survive, reparented to the user `systemd`, still in the group |
| same, continued | the 2 orphans | `os.killpg(pgid, SIGKILL)` | all gone |
| Four `-sa` variants | not listed (the driver logged no tree) | `killpg(SIGKILL)` or own exit | no survivors |

- On Linux the `uv tool` entry point is a symlink to a console script whose shebang is the tool
  venv's Python, so there is no separate shim process; owocr's own `multiprocessing` children (spawn
  start method, `__main__.py:18`) are the reason a parent-only kill is not enough.
- `-t False` matters: with the tray on, owocr preloads the picker in a child at start-up
  (`run.py:1896,1928-1929`).
- No owocr 1.26.8 code calls `setsid`, `setpgid` or `start_new_session` (grep), so every descendant
  stays in the group. `subprocess.run` at `ocr.py:946,1899` is Windows/Screen AI helper code.
- Windows (uv trampoline `.exe` launching `python.exe`, job object with kill-on-close) is not
  testable here: H5.

## Install floor

- `requires-python = ">=3.11"` (`pyproject.toml:10`). Extra `meikiocr = ["meikiocr>=0.3.2"]`;
  resolved 2026-09-21: meikiocr 0.3.4, onnxruntime 1.30.0, numpy 2.5.3, opencv-python-headless
  5.0.0.93, mss 10.2.0, websockets 17.1, obsws-python 1.8.0 (43 packages). Only owocr is pinned; the
  rest float with PyPI.
- **Linux fails as specified.** owocr depends on `PyGObject` on Linux (`pyproject.toml:56`), and
  uv found no Linux wheel for it or for its `pycairo` dependency, so it built both from source. The
  build stopped at `Dependency "cairo" not found` (meson, pycairo 1.29.1;
  `.orchestration/m0/uv-tools/install.log:70`). A Linux user needs a C toolchain plus
  the cairo and GObject-introspection development packages (Debian: `libcairo2-dev`,
  `libgirepository-2.0-dev`) before the add-on can install. `sudo` is not available here, so the
  full list was not confirmed.
- `PyGObject` is used only by the Wayland capture shim (`wayland_mss_shim.py:8-10`, imported when
  `XDG_SESSION_TYPE=wayland`, `run.py:28,94-96`). With `--overrides` mapping
  `pygobject; sys_platform == "never"` the install succeeds (`uv-tools/install-override.log`: 43
  packages, one download prepared in 3.03 s, the rest from uv's cache) and X11 capture works, as
  this spike shows. Wayland capture would then fail at import.
- Size (`du -sh --apparent-size`, after the runs): tool venv 366 MB; meikiocr models 45 MB in the
  Hugging Face cache. onnxruntime also writes `.cache/Microsoft/DeveloperTools/.onnxruntime/`
  (`deviceid`, `onnxruntime.db`; the path string is in `libonnxruntime.so.1.30.0`). Both landed in
  the scratch `HOME`, where `XDG_CACHE_HOME` also pointed, so which of the two they follow is open.
- uv puts its managed CPython in `~/.local/share/uv/python` and its cache in `~/.cache/uv` unless
  `UV_PYTHON_INSTALL_DIR` / `UV_CACHE_DIR` say otherwise; `UV_TOOL_DIR` alone does not keep the
  add-on inside `~/.anki_miner_game/addons/ocr/`.

## Config file and start-up side effects

- `Config.__init__` reads `os.path.expanduser('~')/.config/owocr_config.ini` (`config.py:110,186`).
  If the file is missing it creates `~/.config/` and **downloads**
  `https://github.com/AuroraWright/owocr/raw/master/owocr_config.ini` into that path
  (`config.py:188-195`) and logs `A default config file has been downloaded to <path>`
  (`run.py:3110`). The first run's log was not kept. What survives: the scratch `HOME` holds
  `.config/owocr_config.ini` (8233 bytes, 14:58:14), byte-identical to `owocr_config.ini` in the
  clone, which the harness never writes; every kept run logs `Parsed config file` (`run.py:3106`).
- So a plain launch writes the user's `~/.config/owocr_config.ini` when they have none and reads it
  when they do, contradicting spec 3.4 and 14 in practice even though the app itself never opens
  the file. It also makes behaviour depend on whatever GitHub master holds on first launch.
- With `HOME` pointed at a scratch dir the real `~/.config/owocr_config.ini` stayed absent (the
  second R3 instance's check, and `owner-check.sh` after the spike). From source, not run: a
  pre-seeded file containing only `[general]` makes owocr parse it, skip the download and take
  everything else from the command line and its defaults (`config.py:199-217`; `get_general`,
  `config.py:209-211`, prefers CLI values).
- Every start also contacts `pypi.org` (latest-version check, `run.py:3038-3044`, 5 s timeout) and
  tries to fetch Chrome Screen AI from `chrome-infra-packages.appspot.com` into `~/.config/screen_ai`
  (`ocr.py:854,933`) even with `-e meikiocr` (both in `out-ocr/ocr-run.log`; the Screen AI fetch
  failed there). Both sit in bare `try`/`except` blocks (`run.py:3038-3044`, `ocr.py:935-949`), so
  a failure only logs; an offline start was not run. Other engine paths also hang off `~`: `~/.config/oneocr` (`ocr.py:1876`, OneOCR files copied from the Snipping
  Tool on Windows 11), `~/.config/google_vision.json`, `~/.config/ndlocr_lite`.

## Timing

Explicit-rectangle run (`out-ocr/`), models cached, `screen_capture_delay_seconds` and
stabilisation at their defaults. The first-ever run, with the model download, was not kept:

| Event | Time |
|---|---|
| spawn -> websocket accepts | at most 1.0 s (the driver retried once a second); the server starts before the engines load |
| spawn -> first frame (text already on screen) | 3.4 s |
| text change -> frame, line 2 | 65 ms |
| text change -> frame, line 3 | 54 ms |

The change -> frame rows compare the window's `SHOW` time (`out-ocr/window-show.log`) with the
driver's receive time (`out-ocr/ocr-events.jsonl`); both are `time.monotonic()` on one host. Line 1
was already on screen when owocr started, so it has no change -> frame figure.

A separate quantity is owocr's own recognition time, from its log (`Text recognized in <s>s`,
`out-ocr/ocr-run.log`): 62, 60 and 49 ms for lines 1-3. It is part of the change -> frame time, not
a measure of it.

Frames are the recognised text only, one websocket text message per line. The lines were instant
swaps; how stabilisation delays typewriter-style text is T32's (H4) question.

## Proposed spec amendments (for the M0 gate)

1. **14 / 3.4, config isolation.** Launch owocr with `HOME` (POSIX) and `USERPROFILE` (Windows) set
   to `~/.anki_miner_game/addons/ocr/home/`, pre-seeded with `.config/owocr_config.ini` holding only
   `[general]`. Replace "the app never reads or writes `~/.config/owocr_config.ini`" with "neither the
   app nor its owocr child touches the user's file". Windows `USERPROFILE` redirection and OneOCR's
   `~/.config/oneocr` copy under it are H5 checks.
2. **14, install.** Set `UV_PYTHON_INSTALL_DIR` and `UV_CACHE_DIR` under `addons/ocr/` alongside
   `UV_TOOL_DIR`/`UV_TOOL_BIN_DIR`; add a constraints file pinning the resolved transitive set
   (meikiocr, onnxruntime, numpy, opencv-python-headless, mss) so an install is reproducible.
3. **14, Linux install (may need an owner call).** The specified `uv tool install` fails on a Linux
   host without cairo/GObject-introspection dev packages, and without `PyGObject` owocr cannot
   capture on Wayland, which is most current Linux desktops. Options: (a) document the dev packages
   as a Linux prerequisite and show uv's build error in the add-on banner; (b) install with
   `pygobject` overridden out and support X11 sessions only; (c) mark OCR on Linux unsupported in
   v1. Linux is best-effort (Global Constraints), so (a) or (c) fits; the owner decides.
4. **14, supervisor and picker.** Start owocr with `start_new_session=True` and kill with
   `os.killpg` (confirmed necessary: a parent-only kill orphans the picker child and the
   `resource_tracker`). Exit code 1 with a known message (`Invalid coordinate set(s)`, `Window
   capture is only currently supported`, `Picker window was closed`) is a configuration error, not a
   crash: show a banner instead of spending the three restarts. "Select OCR area" kills owocr after
   the coordinate line, or on picker-closed; the window picker's closed case keeps owocr running
   and prints no coordinate line. Tracebacks in the log (the stdin `termios` one) are not failures.
5. **3.4, cite.** Log lines also at `run.py:2480,2517` (picker) besides `1956,2035` (explicit); the
   window line is Windows-only (Linux never reaches `-swa`).

## Left for H4 / H5

- Picker round trip with a real drag (Linux screen picker, Windows window picker): H4.
- Wayland portal capture and the `-sa <rects>` path under the portal shim: H4 (needs PyGObject).
- Windows: install floor with the `oneocr` extra, the uv trampoline process tree under a job object,
  `USERPROFILE` redirection with OneOCR: H5.
