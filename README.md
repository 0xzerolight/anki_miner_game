<h1 align="center">
  <img src="packaging/icons/anki-miner-game.svg" height="76" align="absmiddle" alt=""> Anki Miner Game
</h1>

<p align="center">
<a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.12+-blue.svg" alt="Python 3.12+"></a>
<a href="https://www.gnu.org/licenses/gpl-3.0"><img src="https://img.shields.io/badge/License-GPLv3-blue.svg" alt="License: GPL v3"></a>
<a href="https://github.com/0xzerolight/anki_miner_game/releases/latest"><img src="https://img.shields.io/github/downloads/0xzerolight/anki_miner_game/total.svg" alt="GitHub downloads"></a>
<a href="https://github.com/0xzerolight/anki_miner_game/stargazers"><img src="https://img.shields.io/github/stars/0xzerolight/anki_miner_game?style=social" alt="GitHub stars"></a>
<a href="https://discord.com/invite/aDtQyZzUVP"><img src="https://img.shields.io/discord/1517634859110240326?logo=discord&logoColor=white&label=Discord&color=5865F2" alt="Discord community"></a>
</p>

<p align="center">
Record a video-game session through OBS and mine it in <a href="https://github.com/0xzerolight/anki_miner">Anki Miner</a> like an anime episode.
</p>

<p align="center">
Please leave a ⭐ star if Anki Miner Game helped you - it helps others find it :).
</p>

## How it works

OBS records the video; the lines your text hooker sends become the subtitle. Each session lands as one same-stem set:

```
<output folder>/<Game>/<Game> - 01.mkv
<output folder>/<Game>/<Game> - 01.srt
<output folder>/<Game>/<Game> - 01.session.json
```

Anki Miner mines that pair as it is. Nothing in Anki Miner needs changing.

## Installation

### Requirements

- **Windows 10 or 11**, or **Linux**. macOS is not supported.
- **OBS Studio 30.0 or newer** ([download](https://obsproject.com/download)). On Linux the Flathub build works too.
- A **text hooker** with a websocket server, the clipboard, or the OCR add-on for games no hooker can read.
- **[Anki Miner](https://github.com/0xzerolight/anki_miner)**, to turn sessions into cards.

Grab the download for your platform from the [latest release](https://github.com/0xzerolight/anki_miner_game/releases/latest):

| Platform | Download |
|----------|----------|
| Windows | `AnkiMinerGame-*-Windows-x86_64-Setup.exe` |
| Linux (Debian/Ubuntu) | `anki-miner-game_*_amd64.deb` |
| Linux (other) | `AnkiMinerGame-*-Linux-x86_64.AppImage` or `.tar.gz` |

### First-run notes (unsigned builds)

- **Linux AppImage**: make it executable before running it - `chmod +x AnkiMinerGame-*.AppImage`, or **Properties** -> **Allow executing** in your file manager.
- **Windows SmartScreen**: **More info** -> **Run anyway**.
- **Windows Defender false positive**: restore from **Protection history** or [report to Microsoft](https://www.microsoft.com/en-us/wdsi/filesubmission).

<details>
<summary><strong>Install from source (Python 3.12+)</strong></summary>

```bash
git clone https://github.com/0xzerolight/anki_miner_game.git
cd anki_miner_game
uv sync
uv run anki_miner_game
```

For full development setup, see [CONTRIBUTING.md](CONTRIBUTING.md).

</details>

## Recording a session

1. **First run**: the setup wizard sets up OBS, checks your text sources, picks the output folder and offers the add-ons. OBS gets its own `Anki Miner Game` profile and scene collection; yours are untouched.
2. **New game…**, pick it, then **Arm**. OBS switches to the app's profile and the text sources connect.
3. **Start**, play, **Stop**. The session moves to its game folder and a panel says how to mine it.
4. **Disarm** when done. OBS goes back to your own profile and scene collection.

Starting and stopping from OBS's own window works the same while armed. Pause with OBS's Pause button; the subtitle stays in step.

### Text hookers

Turn on the ones you use in **Settings…** -> **Text sources**.

| Hooker | Default address | In the hooker |
|--------|-----------------|---------------|
| Textractor | `localhost:6677` | Add a websocket extension listening on port 6677 |
| Agent | `localhost:9001` | Turn on its websocket server |
| LunaTranslator | `localhost:2333` | Turn on its network service |

GameSentenceMiner-style JSON (`{"sentence": ...}`) works too. **Clipboard** (per game) works on Windows and X11; on Wayland use a websocket hooker.

### Start and stop from the keyboard

- **Windows**: `Ctrl+Shift+F9` while armed (**Hotkey (Start/Stop while armed)** in **Settings…**).
- **Linux**: bind the app's command with `--toggle`, `--start`, `--stop` or `--arm <game>` in your desktop's shortcut settings. Works on Wayland. The command depends on the download:
  - `.deb`: `anki_miner_game --toggle`
  - `.AppImage`: `/home/you/Applications/AnkiMinerGame-<version>-Linux-x86_64.AppImage --toggle`
  - `.tar.gz`: `/home/you/Apps/AnkiMinerGame/anki_miner_game --toggle`
- **Windows** takes the same verbs, for a shortcut or a script: `"%LOCALAPPDATA%\Programs\AnkiMinerGame\AnkiMinerGame.exe" --toggle`

`<game>` is the game's profile name in `~/.anki_miner_game/games/`, without `.json`.

## Mining in Anki Miner

- One session: Video -> Single, choose the `.mkv`; the subtitle fills in by itself.
- A whole game: Video -> Batch, choose the game folder as both the video and the subtitle folder.

The folder name becomes the series and the file name the episode. Recommended Anki Miner settings:

- Settings -> Card Media -> Audio Padding: 0.3 seconds. The app's **Gap before the next cue** assumes it; raise both together.
- Settings -> Card Media -> Screenshot Offset: 1.0 seconds.
- Settings -> Filtering -> Deduplicate by Sentence: on, since games repeat lines.

## Features

- Automatic OBS setup on the app's own profile and scene collection.
- Game profiles - per-game text sources, capture window, audio (the game window only on Windows, or the whole desktop) and line filters.
- Auto mode - start at the first line, stop after idle minutes or when the pinned game window closes.
- Voice detection add-on (VAD) - trims each cue's end to where the voice stops; **Restore untrimmed subtitle** puts the live one back.
- OCR add-on (owocr) - for games no hooker can read. OneOCR (Windows) and meikiocr run locally; Google Lens and Bing are cloud engines, off unless you choose them.
- Text feed - each line on a local page at `http://127.0.0.1:6679` for Yomitan lookups; texthooker pages can connect to `ws://127.0.0.1:6678`.
- Crash recovery - an interrupted session is finished at the next launch, and OBS goes back to your profile.

<details>
<summary><strong>OCR add-on notes</strong></summary>

- On Linux OCR needs an X11 session.
- owocr's websocket server listens on `0.0.0.0`, so other machines on your network can read the OCR text while it runs. You can refuse the Windows firewall prompt.
- If the app crashes during OCR on Linux, owocr keeps running. Find it with `pgrep -af '.anki_miner_game/addons/ocr/'` and end it with `pkill -KILL -f '.anki_miner_game/addons/ocr/'`.

</details>

## Troubleshooting

Problems show as banners in the main window.

| Issue | Solution |
|-------|----------|
| Where are the logs? | `~/.anki_miner_game/anki_miner_game.log` (`%USERPROFILE%\.anki_miner_game\` on Windows). |
| OBS did not answer within 30 s | OBS may be waiting on a dialog in its own window, such as "OBS Studio Crash Detected". Answer it, then **Arm** again. |
| OBS's websocket server is off | Close OBS and press **Fix**, or turn it on in OBS (Tools -> WebSocket Server Settings) and press **Fix**. |
| OBS rejected the websocket password | In OBS, Tools -> WebSocket Server Settings -> Show Connect Info; type that password in **Settings…** -> **OBS** -> **Password**. |
| Arm refused: OBS is streaming, recording, or running its replay buffer or virtual camera | Stop that output in OBS, then **Arm** again. |
| OBS is asking to restart | The profile switch changed the audio sample rate. Answer in OBS's window; No keeps OBS running. |
| No text source is connected | Start the hooker or check its port in **Settings…**. The recording carries on meanwhile. |
| No lines were recorded | The video is kept without a subtitle. |
| OBS split the recording into a second file | Lines after the split get no subtitle. Leave splitting off in the app's profile. |
| "Not moved yet; retried at next launch" | The video was still in use; the app moves it at the next launch. |
| Text feed off: port in use | Change **Page port** or **WebSocket port** in **Settings…**. |
| OBS stayed on the app's profile | Start the app again, or pick your own profile and scene collection in OBS. |
| Wrong episode number in Anki Miner | Keep nothing but the app's own files in the game folder. |
| Disk space | About 2.78 GB per hour at OBS's default encoder settings. **Arm** warns below 5 GB free. |

## Contributing

Contributions of any kind are welcome.

- New here? Start with [CONTRIBUTING.md](CONTRIBUTING.md).
- Architecture overview: [ARCHITECTURE.md](ARCHITECTURE.md).
- Code of Conduct: [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
- Security: [SECURITY.md](SECURITY.md).

Bug reports and feature requests -> [Issues](https://github.com/0xzerolight/anki_miner_game/issues).
General questions and discussion -> [Discord](https://discord.com/invite/aDtQyZzUVP).

See [CONTRIBUTORS.md](CONTRIBUTORS.md) for everyone who has contributed.

## License

GNU General Public License v3.0 only. See [LICENSE](LICENSE).
