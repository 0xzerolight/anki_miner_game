# H5: manual QA before a release

Human gate H5 of the master plan: spec 18.4, every Windows check deferred by owner decision D2, the
PipeWire capture row and the Wayland clipboard check. Run it on the release candidate bundles, not
from source, after `scripts/release_dryrun.sh` has printed `RELEASE DRY-RUN GREEN`.

Tick each box, note the build, OBS version and machine, and write every measured value next to its
item. A value marked "provisional" in the code is confirmed or corrected here: the location to update
is given in brackets. A failure is a release blocker unless the owner rules otherwise.

Machines: one Windows 10 or 11 PC (Windows 10 build 19041 or later for application audio) and one
Linux desktop, each with OBS 30.0 or newer, Anki Miner, and a free voiced and a free unvoiced visual
novel demo.

## 1. Sessions and mining (spec 18.4), on Windows and on Linux

- [ ] Windows, Textractor with a websocket extension: a voiced session records and finalises.
- [ ] Windows, Agent: a session records and finalises.
- [ ] Windows, LunaTranslator: a session records and finalises.
- [ ] Linux: the same three hookers in turn, where each runs (through Wine or Proton where needed).
- [ ] One voiced and one unvoiced visual novel demo among the sessions above.
- [ ] One OCR session per platform (Linux on X11).
- [ ] Anki Miner, Video -> Single: choosing the `.mkv` fills in the `.srt` by itself; the session
      mines.
- [ ] Anki Miner, Video -> Batch over a folder of three sessions of one game: every session pairs
      and mines, numbered `01`, `02`, `03`.
- [ ] Ten cards checked by ear and eye: the audio starts with the voice, ends with it, and the
      screenshot shows the right line.
- [ ] Cards and statistics read `<Game>` / `<Game> - NN` (spec Appendix A).

## 2. Sync probe (spec 18.3)

- [ ] Linux: `tools/sync_probe` against the app (`analyse.py --app`), every flash under 150 ms,
      a pause included. E1 passed on 2026-09-22 (27 of 27, largest +37 ms); re-run on the
      release build.
- [ ] Windows: the same with `game_capture`, every flash under 150 ms. This is also the M1 exit on
      Windows (D2).
- [ ] Windows zero event and latency: the flashes confirm `STARTED` as the zero and 10 ms latency,
      with the spread across two encoders (one hardware) under 100 ms; if not, calibration is
      designed before release (spec 21). [`session/session.py` `ZERO_EVENT`,
      `CAPTURE_LATENCY_MS`]
- [ ] Windows: `time.get_clock_info("monotonic").resolution` on the shipped Python; note it. A
      tick near 15.6 ms adds that much error to every line (`docs/m0/clock.md` Limits).

## 3. Windows checks (D2)

- [ ] **M1 exit**: a Textractor session on Windows produces a pair that Anki Miner mines
      (section 1 covers it; tick here when it did).
- [ ] **Rename lock**: after `STOPPED` the finalise rename succeeds; note how long OBS held the
      file. The retry window is about 10 s. [`session/finalise.py` `RENAME_BACKOFF_S`]
- [ ] **Rename still locked**: hold the `.mkv` open in another program across Stop; the row reads
      "Not moved yet; retried at next launch", and the next launch moves it.
- [ ] **Hotkey, fullscreen**: `RegisterHotKey` fires Start and Stop while a fullscreen game has
      focus.
- [ ] **Hotkey, elevated**: the same while a game run as administrator has focus. If it does not
      fire, the user guide says so.
- [ ] **Application audio**: with a pinned window and **The game window only**, the
      `wasapi_process_output_capture` input records that game's audio and nothing else; its window
      string equals the `game_capture` one (`<title>:<class>:<exe>`, `#22` and `#3A` escaped).
- [ ] **Game capture**: the default "any fullscreen application" mode records the game; with a
      pinned window, the `window_capture` fallback records a game that refuses the hook.
- [ ] **Discovery**: OBS is found through `%ProgramFiles%\obs-studio\bin\64bit\obs64.exe` and,
      for an install elsewhere or the Steam build, through the registry key
      `HKLM\SOFTWARE\OBS Studio` (64-bit then 32-bit view). [`obs/discovery.py`, provisional block]
- [ ] **Launch**: Arm with OBS closed starts `obs64.exe --minimize-to-tray` from its `bin\64bit`
      folder; `tasklist` sees it running; `GetVersion` answers within 30 s.
- [ ] **Auto-stop, window closed**: closing the pinned game stops the session after two 5 s polls;
      minimising it does not; a game that changes its title while recording keeps recording (class
      plus exe rule).
- [ ] **OCR install**: the OCR add-on installs with the `oneocr` extra; note the size and time.
- [ ] **OCR home**: owocr runs with `USERPROFILE` pointed at the add-on's private home, OneOCR
      works there, and `~/.config/owocr_config.ini` is neither created nor changed.
- [ ] **OCR window picker**: **Select OCR area** with a **Game window title** opens owocr's window
      picker and the area is stored (`Selected window coordinates:`).
- [ ] **OCR process tree**: Stop, Disarm and Quit each end every owocr process; killing the app from
      Task Manager ends them too (job object with kill-on-close). [`addons/ocr_addon.py`]
- [ ] **Firewall**: the prompt for owocr's `0.0.0.0` server appears as the user guide describes, and
      OCR still works when it is refused.
- [ ] **Paths**: a game title with CJK characters and one near `MAX_PATH` in a deep output folder;
      note what happens (spec 10.1 known limits).

## 4. Linux checks

- [ ] **PipeWire capture**: on a Wayland session, capture method **Screen Capture (PipeWire)**. OBS
      shows the portal picker once, keeps the restore token, and later arms record without asking;
      the recording is not black; the sync probe passes through it.
- [ ] **Wayland clipboard**: with **Clipboard** ticked, lines copied while the game has focus are
      missed and lines copied while the app's window has focus arrive, which is what the wizard's
      text says. Note anything that differs.
- [ ] **X11 capture**: on a real X11 desktop, **Window Capture (X11)** of a pinned window records
      the game, not black (R1 side finding 1); a retitled window keeps recording.
- [ ] **Flatpak OBS**: discovery, launch (`flatpak run com.obsproject.Studio --minimize-to-tray`)
      and the config root under `~/.var/app/com.obsproject.Studio/`.
- [ ] **CLI verbs**: `anki_miner_game --arm <slug>`, `--start`, `--stop` and `--toggle` bound to
      desktop shortcuts work on Wayland.
- [ ] **OCR on Wayland**: the add-on says Wayland is not supported, and nothing starts.
- [ ] **owocr left running**: kill the app with `kill -9` during an OCR session; owocr keeps
      running; the `pgrep` and `pkill` commands in the user guide (section 9) find and end it.

## 5. Both platforms

- [ ] First-run wizard from a clean home, every step, including an add-on install.
- [ ] OBS is back on the user's own profile and scene collection after Disarm, after Quit, and
      after the app is killed while armed and started again.
- [ ] A user profile at 44.1 kHz: arming and disarming never stop at OBS's restart question.
- [ ] Disk rate of a one-hour session on the app's profile; note GB per hour at 1080p30 and 720p30
      and put the number in the user guide, section 11.
- [ ] VAD add-on: a finished session trims; **Re-run VAD** and **Restore untrimmed subtitle** work.
