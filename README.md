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
Record a video-game session through OBS and mine it in <a href="https://github.com/0xzerolight/anki_miner">Anki Miner</a>.
</p>

<p align="center">
Please leave a ⭐ star if Anki Miner Game helped you - it helps others find it :).
</p>

## Installation

### Requirements

- **Windows 10 or 11**, or **Linux**. macOS is not supported.
- **OBS Studio 30.0 or newer** ([download](https://obsproject.com/download)). On Linux the Flathub build works too.
- A **text hooker**: Textractor with a websocket extension on port 6677, Agent with its websocket server on, or LunaTranslator with its network service on. Or the clipboard, or the OCR add-on for games no hooker can read.
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

## Features

- Automatic OBS setup on the app's own profile and scene collection; yours are untouched.
- Game profiles - per-game text sources, capture window, audio and line filters.
- Auto mode - start at the first line, stop after idle minutes or when the pinned game window closes.
- Start and stop from the app, OBS, the tray, a hotkey (Windows) or the command line (`--toggle`, `--start`, `--stop`, `--arm <game>`).
- Voice detection add-on (VAD) - trims each cue's end to where the voice stops.
- OCR add-on (owocr) - OneOCR (Windows) and meikiocr run locally; Google Lens and Bing are cloud engines, off unless you choose them.
- Text feed - each line on a local page at `http://127.0.0.1:6679` for Yomitan lookups.
- Crash recovery - an interrupted session is finished at the next launch, and OBS goes back to your profile.

<details>
<summary><strong>How It Works</strong></summary>

1. **Arm** a game. OBS switches to the app's profile and your text sources connect.
2. **Start**, play, **Stop**. The lines your hooker sends become the subtitle.
3. **The session lands as a same-stem pair**: `<Game>/<Game> - 01.mkv` and `<Game> - 01.srt`.
4. **Mine it in Anki Miner**: Video -> Single for one session, or Video -> Batch with the game folder for all of them.

</details>

<details>
<summary><strong>OCR add-on notes</strong></summary>

- On Linux OCR needs an X11 session.
- In a voiced game, set the game's text speed to instant and turn off any option that shows voiced text in step with the voice (STEINS;GATE: 音声同期). A line that types out slowly reaches the app only once it is fully shown, often after its voice has ended, so the card's audio starts late.
- Start the game before **Arm**. On Windows owocr reads the first window whose title contains the profile's **Game window title**, so with the game closed it could read another window, such as a browser tab about the game.
- owocr's websocket server listens on `0.0.0.0`, so other machines on your network can read the OCR text while it runs. You can refuse the Windows firewall prompt.
- If the app crashes during OCR on Linux, owocr keeps running. End it with `pkill -KILL -f '.anki_miner_game/addons/ocr/'`.

</details>

## Troubleshooting

Problems show as banners in the main window.

| Issue | Solution |
|-------|----------|
| Where are the logs? | `~/.anki_miner_game/anki_miner_game.log` (`%USERPROFILE%\.anki_miner_game\` on Windows). |
| Disk space | About 0.3 GB per hour at 1080p30 for a visual novel; more for games with more motion. **Arm** warns below 5 GB free. |

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
