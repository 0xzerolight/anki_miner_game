# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **Session recording through OBS.** Each session lands as `<Game>/<Game> - NN.mkv` with a same-stem `.srt` and `.session.json`, the pair Anki Miner mines like an anime episode.
- **Automatic OBS setup.** The app runs OBS on its own `Anki Miner Game` profile and scene collection while a game is armed; your own profiles and stream settings are untouched.
- **Text sources.** Textractor, Agent and LunaTranslator websockets, GameSentenceMiner-style JSON, and the clipboard.
- **Start and stop from anywhere.** The app window, OBS's own window, the tray, a Windows hotkey, or `--toggle`, `--start`, `--stop` and `--arm` on the command line.
- **Game profiles.** Per-game text sources, capture window, audio source and line filters.
- **Auto mode.** Starts at the first line; stops after idle minutes or when the pinned game window closes.
- **Voice detection add-on (VAD).** Trims each cue's end to where the voice stops; the untrimmed subtitle can be restored.
- **OCR add-on (owocr).** For games no text hooker can read.
- **Text feed.** A local page that shows each line as it arrives, for Yomitan lookups during play.
- **Crash recovery.** An interrupted session is finished at the next launch, and OBS goes back to your profile.
- **Installers.** Windows `Setup.exe`; Linux `.AppImage`, `.deb` and `.tar.gz`.
