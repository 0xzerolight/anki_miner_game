# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- **OBS installed from Steam is found on Windows.** With only Steam's OBS installed, **Set up OBS** said OBS Studio was not installed; the app now also looks where Steam records the install.
- **Cards no longer lose the first words of a voiced line.** In hook mode each cue now starts 0.4 s before its line arrives: games start the voice before drawing the text, and hookers send the line later still (LunaTranslator about 0.24 s after the first glyph, Textractor about 0.5 s), so the clip began after the voice had started.
- **A game window smaller than the screen now fills the recording and card screenshots.** OBS placed it unscaled in the top-left corner with black around it; each capture input is now fitted to the canvas, aspect kept and centred.
- **A windowed game with no window pinned is recorded on Windows, not a black screen.** Automatic capture now keeps a Display Capture of the primary monitor underneath Game Capture, which records only fullscreen games.
- **Agent's machine translation no longer lands in the subtitle.** With Agent's default settings (Machine Translate on), its English translation frame reached the pipeline as its own line beside the hooked Japanese one; the translation frame is now dropped.
- **The speaker-tag filter (Game profile -> Remove a leading 【name】 speaker tag) now also strips a `name: ` prefix (ASCII colon, one space) before an opening quote.** The Agent hooker's STEINS;GATE script sends dialogue as `倫太郎: 「…」` rather than LunaTranslator's `【倫太郎】「…」`; only the bracketed form was removed before, so the speaker name leaked into every cue and card. A full-width colon is left alone, so narration and labels (`注意：これは…`, `太郎はこう言った：「行くぞ」`) are unaffected.
- **Add-on installs work on a new Windows PC.** The uv and voice-model downloads, and the bundle's HTTPS self-check, now check certificates through the OS verifier (`truststore`); on Windows that is the chain check browsers use, which fetches a trusted root the machine does not hold yet from Windows Update. A fresh Windows 11 root store lacks Sectigo Public Server Authentication Root E46, the root github.com chains to, and Python's own check only reads the store, so the VAD and OCR installs failed with `CERTIFICATE_VERIFY_FAILED` until some other program had made Windows fetch that root.
- **A session with a very deep output folder now finishes.** The subtitle and manifest are written through a short, fixed-length temporary file name instead of one built from the target file's own name; on Windows a long game title plus a deep `output_root` could push that temporary name a few characters past the 260-char path limit while the final file stayed under it, so the video moved into the game folder but the subtitle and manifest stayed behind in `_incoming/` and every launch retried and failed the same way. A finalise failure is now also logged, not just shown as a banner.
- **A voiced line with a long pause no longer loses its end to the VAD trim.** A pause of more than 1.5 s ended the line there, so the subtitle and the card's audio stopped mid-line; a pause of up to 2 s now stays inside the line.
- **OCR sends whole lines, also while another window is in front.** owocr sent whatever had changed as soon as two screenshots matched, so a line the game types out character by character arrived as fragments of one to four characters, and its line recovery added the previous half-read line back, doubling lines. It now waits until the text has stayed unchanged for 1 s and recovers no lines: on a STEINS;GATE test run 94 % of lines got one clean cue instead of 47 %. Lines arrive about 1 s later. On Windows it also read nothing while the game window was not the foreground window, such as while the text feed page was in front; it now reads the game window whichever window is in front.
- **OCR keeps its area after the game window is minimised.** On Windows, minimising the game window, or any change of its size, made owocr drop the OCR area and read the whole window until the game was disarmed and armed again, so every later cue carried scene text and name plates. The app now drops those whole-window lines and, once the window is back on screen, restarts owocr, which reads the area again; this restart shows no banner and does not count towards the three restarts after a crash.

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
