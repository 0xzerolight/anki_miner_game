# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- **One click records.** In the main window **Start recording** gets OBS ready and starts the recording in one press, and **Stop recording** leaves the game ready for the next session; **Done playing** puts OBS back on your own profile. **Get ready** appears only for a game whose auto mode starts at the first line. While a step runs the button reads **Getting OBS ready…**, **Starting…** or **Stopping…**, so a second click no longer queues a second setup. The word "arm" is gone from the app.
- **The default Windows hotkey is now `Alt+F9`.** With `Ctrl+Shift+F9` the game saw a held Ctrl, which visual novels such as STEINS;GATE read as skip: every press from inside the game skipped lines and left one merged junk line in the subtitle. Settings that still hold `Ctrl+Shift+F9` change to `Alt+F9` at the next launch; a hotkey you chose yourself is kept.
- **A simpler main window.** No menu bar: **Settings…** sits at the top right. With no game yet the window shows only **Add your game…**. Lights appear once a game is getting ready: **OBS** and one **Game text** light. One status line ("Ready", "Recording 0:22:05 · 7 lines"), larger text in **Lines**, and banners in the theme's text colour with an icon. OBS banners carry **Set up OBS…**. The window fits the screen.
- **Recent sessions say what state a session is in.** Four columns and plain words ("Ready", "Trimming 75%", "Trim failed", "Not filed yet"; hover for why). Double-click a row to open its folder; right-click for **Trim again** and **Undo trimming**, offered only while the voice-trimming add-on is installed. The panel after a session is shorter and points at Video -> Batch once the game has two sessions.
- **The tray follows the window.** It offers the window's buttons and hides what cannot run, its icon shows a state dot, and the first close to the tray says the app is still running.
- **The app has its own icon everywhere.** The window, the taskbar, the tray, `AnkiMinerGame.exe`, Setup and its shortcuts show the app icon; the Windows build carried PyInstaller's default one.
- **Setup takes three pages: OBS, Game text and Optional extras.** The output folder page is gone; the last page says where sessions are saved (change it in **Settings…**). **Set up OBS…** on a banner or in Settings, **Test…** and **Install…** open their page alone.
- **Settings has two groups and Advanced.** **Recordings** and **While playing**, then **Advanced** for the text hookers, the OBS connection, subtitle timing and the text feed ports. One **Video** choice replaces height and frame rate; the hotkey is set by pressing it; Linux shows the exact command to bind. **Test…** checks the hookers as they are in the table, before saving.
- **The game profile asks less.** **Text from** is one choice (a text hooker, copied text, or reading the screen); one **Game window** dropdown sets the window and which sound is recorded; the OCR language is chosen by name; **Auto mode** shows its options only while it is on; the rest is under **Advanced**. A new Windows game can be saved with only its title.
- **`--arm` takes the game's title.** `--arm "Steins;Gate"` works as well as the slug, which no screen shows.

### Fixed

- **The game dialog lists windows when OBS was closed at start.** Opening **Game window** starts OBS minimised and connects ("Starting OBS… (up to 30 s)", then "Looking for windows…"). Before, the list said OBS was not connected, so a new Windows game could not be given its window.
- **Start recording relaunches OBS.** With OBS closed or crashed while a game was ready, every Start failed with "not connected to OBS", and in auto mode every line repeated it. Start now starts OBS and reconnects first; if that fails, the start-failed banner shows and auto mode tries again at the next line.
- **A manual Stop is no longer undone by auto mode.** With auto mode starting at the first line, the next line started a new session right after you pressed Stop. A Stop from the window, the tray, the hotkey or OBS now pauses auto-start until you press **Start recording** or **Get ready**; auto mode's own stops and OBS closing do not.
- **Changes are no longer half-applied while a game is ready.** **Edit…** is off from **Start recording** until **Done playing**, and Settings refuses a new output folder then ("Press Done playing before changing the folder"). Profile edits saved while ready were ignored until the next session although the window already showed them, and a finishing session went to the old folder and never appeared in Recent sessions.
- **OCR lines are no longer lost when owocr starts.** The OCR source now retries every 0.25 s instead of backing off for up to 10 s, during which the line on screen when owocr came up, or came back after a minimise, was dropped.
- **Trim again no longer undoes a good trim.** A re-run that cannot finish, because the add-on is damaged or the trim fails, keeps the session's trimmed subtitle.
- **Installing or repairing the OCR add-on no longer freezes the lights and lines.** Removing the old install ran on the app's I/O thread.
- **A second launch no longer becomes a second app.** Two launches close together (a double-click on a pinned icon) started two full instances, and a launch during a quit re-showed a window that then vanished. The second launch now waits for the first and hands it its command.
- **A command such as `--toggle` from a desktop shortcut is no longer dropped now and then.** The running app could lose a command it had just accepted, and the shortcut did nothing.
- **A failed write to the session's line journal no longer leaves the app stuck in Recording.**
- **Done playing or Quit while a recording is starting no longer leaves OBS recording on its own.**
- **Warnings about one session clear when the next recording starts.**
- **Auto-start keeps the newest line** when several arrive at once, instead of starting with a stale one and losing the newer.
- **A failed auto-stop is tried again** at the next check.
- **Installing OBS while the app runs is seen without a restart.**
- **Settings check the output folder and hooker addresses before saving.** A relative folder, one that cannot be created or written, `localhost:` or an `http://` address was saved and then failed.
- **Saving Settings while an OCR or clipboard game is ready keeps its light.**
- **Trim again or Undo trimming on a session moved or deleted meanwhile reloads the list** instead of leaving the row busy.
- **An error during start-up is written to the log.**
- **The uninstaller names the real settings folder** instead of a literal `%USERPROFILE%`.
- **Windows: another user's OBS is not taken for yours.**

### Security

- **The text feed's WebSocket accepts only this machine's pages and the hosted texthooker-ui.** Any web page open in your browser could read the feed, which in clipboard mode carries everything you copy while a game is ready. Apps and scripts (no Origin), pages from `http://127.0.0.1` or `http://localhost`, and `https://renji-xd.github.io` still connect; any other page, and a texthooker page opened as a file, is refused (HTTP 403).

## [1.0.1] - 2026-09-25

Fixes from the Windows release QA on STEINS;GATE: add-on installs on a new PC and from Setup's last page, the opening words and long pauses of voiced cards, windowed capture, OCR text and focus, Agent's lines, Steam's OBS, and two hangs.

### Fixed

- **OBS installed from Steam is found on Windows.** With only Steam's OBS installed, **Set up OBS** said OBS Studio was not installed; the app now also looks where Steam records the install.
- **OCR cards of a voiced line now start before its voice.** Each OCR cue now starts 1.25 s before its line arrives, not 1 s: owocr now waits about 1 s for the text to stop changing, so OCR lines arrive later than the old shift allowed for. On STEINS;GATE with instant text, every voiced line owocr sent on time now starts its card's audio before the voice, instead of about 0.07 s after its start.
- **Cards no longer lose the first words of a voiced line.** In hook mode each cue now starts 0.4 s before its line arrives: games start the voice before drawing the text, and hookers send the line later still (LunaTranslator about 0.24 s after the first glyph, Textractor about 0.5 s), so the clip began after the voice had started.
- **A game window smaller than the screen now fills the recording and card screenshots.** OBS placed it unscaled in the top-left corner with black around it; each capture input is now fitted to the canvas, aspect kept and centred.
- **A windowed game with no window pinned is recorded on Windows, not a black screen.** Automatic capture now keeps a Display Capture of the primary monitor underneath Game Capture, which records only fullscreen games. That Display Capture records the whole monitor, other windows included; pin the game window in its profile to record only the game.
- **Agent's machine translation no longer lands in the subtitle.** With Agent's default settings (Machine Translate on), its English translation frame reached the pipeline as its own line beside the hooked Japanese one; the translation frame is now dropped.
- **The speaker-tag filter (Game profile -> Remove a leading 【name】 speaker tag) now also strips a `name: ` prefix (ASCII colon, one space) before an opening quote.** The Agent hooker's STEINS;GATE script sends dialogue as `倫太郎: 「…」` rather than LunaTranslator's `【倫太郎】「…」`; only the bracketed form was removed before, so the speaker name leaked into every cue and card. A full-width colon is left alone, so narration and labels (`注意：これは…`, `太郎はこう言った：「行くぞ」`) are unaffected.
- **Add-on installs work on a new Windows PC.** The uv and voice-model downloads, and the bundle's HTTPS self-check, now check certificates through the OS verifier (`truststore`); on Windows that is the certificate check browsers use, which fetches a trusted root the machine does not hold yet from Windows Update. A fresh Windows 11 root store lacks Sectigo Public Server Authentication Root E46, the root github.com chains to, and Python's own check only reads the store, so the VAD and OCR installs failed with `CERTIFICATE_VERIFY_FAILED` until some other program had made Windows fetch that root.
- **A session in a deep output folder finishes whenever its files fit Windows's 260-char path limit.** The subtitle and manifest are written through a short, fixed-length temporary file name instead of one built from the target file's own name; on Windows a long game title plus a deep **Output folder** could push that temporary name a few characters past the 260-char path limit while the final file stayed under it, so the video moved into the game folder but the subtitle and manifest stayed behind in `_incoming/` and every launch retried and failed the same way. A finalise failure, or a video still in use when the session is moved, is now also logged, not just shown as a banner.
- **An output folder you may not write to shows a banner at Arm instead of hanging the app.** On Windows, **Arm** now shows the "cannot be written to" banner. With writing to `_incoming` denied by the folder's permissions, **Arm** showed nothing, one CPU core stayed at 100 % and the app could not be quit: Python's temporary-file code on Windows retries a refused file create up to 2^31 times. The app now creates its temporary files itself and gives up at the first refusal, so any other save into such a folder fails at once too.
- **A voiced line with a long pause no longer loses its end to the VAD trim.** A pause of more than 1.5 s ended the line there, so the subtitle and the card's audio stopped mid-line; a pause of up to 2 s now stays inside the line.
- **OCR sends whole lines, also while another window is in front.** owocr sent whatever had changed as soon as two screenshots matched, so a line the game types out character by character arrived as fragments of one to four characters, and its line recovery added the previous half-read line back, doubling lines. It now waits until the text has stayed unchanged for 1 s and recovers no lines: on a STEINS;GATE test run 94 % of lines got one clean cue instead of 47 %. Lines arrive about 1 s later, and a line that types out slowly arrives only once fully shown, so in a voiced game set the text speed to instant (README, OCR add-on notes). On Windows it also read nothing while the game window was not the foreground window, such as while the text feed page was in front; it now reads the game window whichever window is in front.
- **OCR keeps its area after the game window is minimised, and works when armed with the game minimised.** On Windows, minimising the game window, or any change of its size, made owocr drop the OCR area and read the whole window until the game was disarmed and armed again, so every later cue carried scene text and name plates. And with an OCR area selected, an owocr started while the game window was minimised, at **Arm** or at a restart, hung without reading anything and without an error, so the session got no OCR lines at all. The app now drops whole-window lines, runs owocr on the whole window while the game is minimised, and once the window is back on screen restarts owocr, which reads the area again; these restarts show no banner and do not count towards the three restarts after a crash.
- **Arm explains an OBS websocket server that is off, and keeps the "OBS did not answer" banner.** With OBS running and its websocket server off (or OBS in Safe Mode), Arm showed only a raw `ConnectionRefused` error; it now gives the same instructions as the Setup wizard and points at OBS -> Fix there, with a short Safe Mode hint when the server is on but the connection is still refused. And after OBS dies while armed or recording, the next Arm's 30 s "OBS did not answer" banner no longer gets overwritten moments later by the raw error of a reconnect the app tries in the background.
- **The add-ons install, and VAD runs, when Setup's last page started the app.** On Windows, the app started by **Launch Anki Miner Game** (ticked by default) inherited Setup's RedirectionGuard, which stops a program following a folder link a normal user made. uv reaches its Python through such a link, so the VAD and OCR installs failed (`uv venv failed (exit 2)`, `Installing owocr failed`) and every VAD pass failed (`uv trampoline failed to spawn Python child process`) until the app was started again from its shortcut. Setup no longer turns RedirectionGuard on.
- **Setup no longer closes the OBS the app started.** On Windows, OBS started by the app loaded the app's own copy of the Visual C++ runtime (`VCRUNTIME140.dll`) instead of the system's, so Setup's "Preparing to Install" page listed OBS Studio beside the app and closed it on every upgrade or reinstall. OBS now starts with Windows's own DLL search order and without the app's folders on `PATH`. An OBS started by an older version still holds that file until it is closed.

## [1.0.0] - 2026-09-22

First release. Records a video-game session through OBS as the `.mkv` + `.srt` pair Anki Miner mines like an anime episode, on Windows and Linux. Lines come from a websocket text hooker, the clipboard or the OCR add-on, and the voice detection add-on trims each line to where the voice stops.

### Added

- **Session recording through OBS.** Each session lands as `<Game>/<Game> - NN.mkv` with a same-stem `.srt` and `.session.json`, the pair Anki Miner mines like an anime episode.
- **Automatic OBS setup (Setup wizard).** The app runs OBS on its own `Anki Miner Game` profile and scene collection while a game is armed; your own profiles and stream settings are untouched.
- **Text from Textractor, Agent, LunaTranslator, GameSentenceMiner-style JSON and the clipboard (Settings -> Text sources).** The hookers connect over their websocket servers; the clipboard is set per game and works on Windows and X11.
- **Start and stop from the app, OBS, the tray, a Windows hotkey or the command line.** `--toggle`, `--start`, `--stop` and `--arm <game>` on the command line; the Windows hotkey defaults to `Ctrl+Shift+F9`.
- **Game profiles (New game…).** Per-game text sources, capture window, audio source and line filters.
- **Auto mode.** Starts at the first line; stops after idle minutes or when the pinned game window closes.
- **Voice detection add-on (VAD) that trims each line's end to where the voice stops.** **Re-run VAD** and **Restore untrimmed subtitle** work on any finished session.
- **OCR add-on (owocr) for games no text hooker can read.** OneOCR (Windows) and meikiocr run locally; Google Lens and Bing are cloud engines, off unless chosen. On Linux it needs X11.
- **Text feed page for Yomitan lookups during play (Settings -> Text feed).** Each line appears as it arrives at `http://127.0.0.1:6679`; texthooker pages can connect to `ws://127.0.0.1:6678`.
- **Crash recovery.** An interrupted session is finished at the next launch, and OBS goes back to your profile.
- **Windows `Setup.exe` installer; Linux `.AppImage`, `.deb` and `.tar.gz`.**
