# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
