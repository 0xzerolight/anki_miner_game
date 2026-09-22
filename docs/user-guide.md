# Anki Miner Game: user guide

Anki Miner Game records a play session through OBS and writes a subtitle from the lines your text
hooker sends. Each session ends up as a pair in one folder:

```
<output folder>/<Game>/<Game> - 01.mkv
<output folder>/<Game>/<Game> - 01.srt
<output folder>/<Game>/<Game> - 01.session.json
```

Anki Miner mines that pair the way it mines an anime episode. Nothing in Anki Miner needs changing.

In this guide, bold type marks a label in Anki Miner Game's own windows. Labels in OBS, Anki Miner and
the text hookers are written plainly, with `->` for a menu path.

Contents: 1 Requirements, 2 Install, 3 First run, 4 Text hookers, 5 Recording a session,
6 Mining in Anki Miner, 7 Settings, 8 Game profiles, 9 Add-ons, 10 Text feed, 11 Disk space,
12 Troubleshooting.

## 1. Requirements

- Windows 10 or 11, or Linux. macOS is not supported.
- OBS Studio 30.0 or newer, installed by you (<https://obsproject.com/download>). On Linux the
  Flathub build (`com.obsproject.Studio`) works as well as a system package.
- A text hooker with a websocket server (section 4), or the clipboard, or the OCR add-on for games
  no hooker can read.
- Anki Miner, to turn the sessions into cards.

## 2. Install

Download the build for your system from the project's GitHub Releases page:

- Windows: the `Setup.exe` installer.
- Linux: the `.AppImage`, the `.deb`, or the `.tar.gz`.

The app keeps its own files in `~/.anki_miner_game/` (`%USERPROFILE%\.anki_miner_game\` on
Windows): `config.json`, the game profiles in `games/`, the add-ons in `addons/`, and the log,
`anki_miner_game.log`. It never touches OBS files outside its own profile and scene collection,
and it never reads or writes owocr's own `~/.config/owocr_config.ini`.

## 3. First run

The setup wizard (**Anki Miner Game setup**) opens at the first launch. Each step can be run again
later from **Settings…** -> **Setup wizard**, or all of them from **File** -> **Setup wizard…**.

1. **OBS**. The page lists what the app changes in OBS before it changes anything. Press
   **Set up OBS**. The app finds OBS, turns on OBS's websocket server when it is off and OBS is
   closed, starts OBS minimised, and creates a profile and a scene collection, both named
   `Anki Miner Game`. OBS is switched to them only while a game is armed, and back to your own
   profile and scene collection when you disarm.
   - Only the app's own profile is configured: record folder, output size and frame rate, the
     `.mkv` container, file splitting and automatic remux off, a recording encoder separate from
     the stream encoder (so that pausing works), and your audio sample rate and channels. Your
     stream settings and your own profiles are untouched.
   - Creating the profile turns off OBS's offer to run its auto-configuration wizard for new
     profiles. That is OBS's own behaviour.
   - If OBS is running with its websocket server off, the page asks you to tick OBS's
     Tools -> WebSocket Server Settings -> Enable WebSocket server and press OK, then **Fix**. Or
     close OBS and press **Fix**.
   - If OBS is not installed, the page links to the download; install it, then **Check again**.
2. **Text sources**. Start your hooker and play until a line shows. Each enabled source says
   "waiting for a line" until one arrives.
3. **Output folder**. Where finished sessions go; the default is `~/Videos/Anki Miner Game`. OBS
   records into `_incoming/` inside it, and each session is moved to its game folder when it ends.
   Pick a drive with room (section 11).
4. **Optional add-ons**. **Voice detection (VAD)** and **OCR (owocr)**, each with its download
   size. Nothing downloads until you press **Install**. Both can be installed later.

The app reads the OBS websocket port and password from OBS's own settings each time it connects and
stores neither. You only type a password in **Settings…** when OBS's has changed and the banner asks
for it.

## 4. Text hookers

The app listens beside your texthooker page; it does not take lines away from it. Three sources are
set up by default. Turn on the ones you use in **Settings…** -> **Text sources**, or add another
with **Add** (the address is `host:port`, as the hooker's websocket server listens).

| Hooker | Default address | What to do in the hooker |
|---|---|---|
| Textractor | `localhost:6677` | Add a websocket extension (Textractor has none built in) and check that it listens on port 6677 |
| Agent | `localhost:9001` | Turn on its websocket server, port 9001 |
| LunaTranslator | `localhost:2333` | Turn on its network service, port 2333. The app tries Luna's `/api/ws/text/origin` path by itself |

Hookers that send GameSentenceMiner-style JSON (`{"sentence": ...}`) work too; the app takes the
`sentence` field.

Tick **Clipboard** in a game's profile to take lines copied to the clipboard. On
Windows and on X11 this works while you play. On Wayland the app sees the clipboard only while one
of its own windows has focus, so lines copied during play are missed; use a websocket hooker there.

The status row at the top of the main window shows one light per source: grey disconnected, amber
connecting, green connected, blue receiving. Hover a light for its state.

## 5. Recording a session

1. Create the game with **New game…** (section 8) and pick it from the list.
2. Press **Arm**. OBS switches to the app's profile and scene collection, and the text sources
   connect. If OBS is closed, the app starts it minimised and waits up to 30 s for it.
3. Press **Start** and play. The **Lines** list shows each accepted line; the elapsed time and cue
   count run beside the buttons.
4. Press **Stop**. The session is finalised: the subtitle is written and the video, subtitle and
   manifest move to `<Game>/<Game> - NN`. A panel then says how to mine it, with an **Open folder**
   button.
5. Press **Disarm** when you are done. OBS goes back to your own profile and scene collection.

Starting and stopping from OBS's own window works the same while armed. A recording started when no
game is armed is yours: the app leaves it alone.

Pause with OBS's own Pause button. Lines that arrive while paused are dropped, and the subtitle stays
in step with the video after you resume.

The tray menu has **Arm**, **Start**, **Open text feed**, **Show window** and
**Quit**. Closing the window while armed or recording keeps the app in the tray.

- Windows: a global hotkey starts and stops while armed, `Ctrl+Shift+F9` by default
  (**Hotkey (Start/Stop while armed)** in **Settings…**).
- Linux has no global hotkey. In your desktop's keyboard shortcut settings, bind the app's command
  followed by `--toggle`, `--start`, `--stop` or `--arm <game>`; this also works on Wayland. The
  command depends on the download:
  - `.deb`: `anki_miner_game`.
  - `.AppImage`: the full path of the `.AppImage` file.
  - `.tar.gz`: the full path of `AnkiMinerGame/anki_miner_game` in the folder you extracted it to.

  For example:

  ```
  anki_miner_game --toggle
  /home/you/Applications/AnkiMinerGame-<version>-Linux-x86_64.AppImage --toggle
  /home/you/Apps/AnkiMinerGame/anki_miner_game --toggle
  ```

  `<game>` is the game's slug, the file name of its profile in `~/.anki_miner_game/games/` without
  `.json`.
- Windows takes the same verbs after the full path of `AnkiMinerGame.exe`, for a shortcut or a
  script. With the default install folder:

  ```
  "%LOCALAPPDATA%\Programs\AnkiMinerGame\AnkiMinerGame.exe" --toggle
  ```

**Recent sessions** lists the latest sessions with their duration, cue count and VAD state. Each
row has **Open folder**. With the VAD add-on installed, a row with a subtitle also offers
**Re-run VAD** and **Restore untrimmed subtitle** in its right-click menu (section 9).

How a cue is timed: it starts when its line arrives and ends shortly before the next line, or after
**Longest cue** when the next line is slow to come. Lines shown for under 0.3 s (skip mode) are
dropped. With the VAD add-on installed, each cue's end is then trimmed to where the voice stops.

## 6. Mining in Anki Miner

After each session the app shows the hand-off, for example:

> Saved "Steins;Gate - 03". To mine it in Anki Miner: Video -> Single, choose the .mkv; the subtitle
> fills in by itself. To mine every session of this game at once: Video -> Batch, and choose this
> folder for both the video and the subtitle folder.

Anki Miner uses the folder name as the series and the file name as the episode, so cards and
statistics read `Steins;Gate` / `Steins;Gate - 03` with no extra work.

Recommended Anki Miner settings for game sessions:

- Settings -> Card Media -> Audio Padding: leave it at 0.3 seconds. The app's **Gap before the next
  cue** (350 ms) assumes it. If you raise Audio Padding, raise **Gap before the next cue** by the
  same amount.
- Settings -> Card Media -> Screenshot Offset: leave it at 1.0 seconds.
- Settings -> Filtering -> Deduplicate by Sentence: keep it on, since games repeat lines.

Keep only one `.srt` beside each video, and do not leave stray numbered `.srt` files in a game
folder: Anki Miner pairs by the number in the file name.

## 7. Settings

**Settings…** is in the **File** menu.

- **Recordings**: **Output folder**, **Video height at most** (1080 or 720), **Frame rate**.
- **OBS**: **Host**, **Port** and **Password**. Leave port and password at "Read from OBS"; a
  password typed here is stored in the app's settings.
- **Text sources**: the websocket sources, on or off, with **Add** and **Remove**.
- **Subtitles**: **Longest cue** (15 s by default), **Gap before the next cue** (350 ms), and
  **Trim cue ends to the voice (VAD)**, which needs the VAD add-on.
- **Text feed**: **Serve the text feed**, **Page port** (6679) and **WebSocket port** (6678).
- **Start and stop**: the Windows hotkey, or on Linux the commands of section 5.
- **Setup wizard**: run one wizard step again.

## 8. Game profiles

**New game…** and **Edit…** open the game's profile.

- **Title**: the game's name. The folder and file names use a cleaned form of it (characters that
  cannot appear in a file name become spaces, and patterns Anki Miner would read as an episode
  number, such as ` - 3` or `S01E05`, get a `~`), so every session is numbered correctly.
- **Text from**: **Text hooker or clipboard**, or **OCR of the screen (add-on)**. One game uses one
  kind, not both.
- **Text sources**: which hookers this game listens to (**Every source turned on in Settings** by
  default), and **Clipboard**.
- **Record audio of**: **The game window only** (Windows, needs a pinned window) or
  **The whole desktop**.
- **Capture**: **Automatic** picks the capture method; the line below it says which one is in use.
  **List windows**, then choose the game in **Pick**, to pin one window. **Unpin** goes back to the
  default. On Wayland OBS has no window list: it asks which window to capture the first time it
  records, and remembers the answer.
- **Line filters**: **Remove a leading 【name】 speaker tag** (on by default) and
  **Merge lines that are typed out gradually** (off by default; it would also swallow a short real
  line, such as え followed by えっと…).
- **Auto mode**, off by default:
  - **Start recording at the first line**. That line sits at 0:00 and the start of its voice is
    missing from the video; press **Start** yourself before the first line if that matters.
  - **Stop after no line for** a number of minutes; 0 never stops for idling.
  - **Stop when the pinned game window closes**. Windows and X11 only; on Wayland the idle stop
    applies.

## 9. Add-ons

Both add-ons download on demand, into `~/.anki_miner_game/addons/`, and neither is needed.

**Voice detection (VAD)**. After each session it finds where the voice stops and trims each cue's
end there, so cards carry less music. The live subtitle is kept in the manifest, so
**Restore untrimmed subtitle** puts it back and **Re-run VAD** runs the pass again. Only one `.srt`
ever sits beside the video. If the add-on is missing or fails, the live subtitle stands and the
session row says why.

**OCR (owocr)**. For games no text hooker can read. It runs owocr in the background while the game
is armed, reading one area of the screen.

- In the game's profile choose **OCR of the screen (add-on)**, an **Engine** and a **Language**
  (`ja` by default), then **Select OCR area**. owocr's own picker opens; draw the area. On Windows,
  fill **Game window title** first so the area follows the window.
- Local engines: OneOCR on Windows 10 and 11, meikiocr on any system. Google Lens and Bing are
  cloud engines, off unless you choose them: every screenshot of the OCR area then leaves your
  machine for Google's or Microsoft's servers.
- On Linux OCR needs an X11 session. Wayland sessions are not supported.
- owocr's websocket server listens on `0.0.0.0`, so while it runs the OCR text can be read from
  other machines on your local network. On Windows the firewall may ask whether to allow it; you can
  refuse, since the app connects to it on this machine.
- OCR lines arrive a little after the text appears, once it has stopped changing. The app moves
  each OCR cue's start 1 s earlier to make up for it, and the VAD pass can move it to where the
  voice starts.

### Ending an owocr left running on Linux

If Anki Miner Game crashes while OCR is running, owocr
keeps running on Linux, still capturing the screen and serving on `0.0.0.0`. (On Windows it ends
with the app.) To see whether one is left:

```
pgrep -af '.anki_miner_game/addons/ocr/'
```

To end it and its helper processes:

```
pkill -KILL -f '.anki_miner_game/addons/ocr/'
```

This matches only the app's own owocr, not one you run yourself. If you set `ANKI_MINER_GAME_HOME`,
use that folder's path in both commands instead of `.anki_miner_game`.

## 10. Text feed

While the app runs, a page on this machine shows each line as it arrives, for Yomitan lookups
during play. Open it from the tray (**Open text feed**) or at `http://127.0.0.1:6679`. Texthooker
pages can connect to `ws://127.0.0.1:6678` instead. It matters most for OCR and clipboard
sessions, which have no texthooker page of their own. Both ports listen on this machine only.

## 11. Disk space

Measured with OBS's default encoder settings (constant bitrate, 6000 kb/s video and 160 kb/s
audio), a recording takes about 2.78 GB per hour, at 1080p30 and at 720p30 alike: with those
settings the resolution does not change the rate. The app's profile gives recording its own
encoder (on OBS's Simple output mode, the recording quality "Small" instead of "Same as stream"),
which changes the rate; that setting has not been measured, so check the size of a first real
session before a long one.

At **Arm** the app warns when less than 5 GB is free in the output folder. OBS stops the recording
itself when the disk fills.

## 12. Troubleshooting

Problems show as banners in the main window, never as pop-ups. The log is
`~/.anki_miner_game/anki_miner_game.log`.

| What you see | What to do |
|---|---|
| OBS is not installed | Install OBS 30.0 or newer, then **Check again** in the wizard |
| OBS did not answer within 30 s | OBS may be waiting on a dialog in its own window, such as "OBS Studio Crash Detected" after a crash. Answer it, then **Arm** again |
| OBS's websocket server is off | Close OBS and press **Fix**, or turn it on in OBS (Tools -> WebSocket Server Settings) and press **Fix** |
| OBS rejected the websocket password | In OBS, Tools -> WebSocket Server Settings -> Show Connect Info; type that password in **Settings…** -> **OBS** -> **Password** |
| OBS lacks a request; update OBS | The banner names the missing request. Update OBS to 30.0 or newer |
| Arm refused because OBS is streaming, recording, or running its replay buffer or virtual camera | Stop that output in OBS, then **Arm** again |
| A profile or scene collection switch timed out | OBS is put back on your own profile and collection. Try **Arm** again |
| OBS is asking to restart | OBS changed profile to one with a different audio sample rate. Answer the question in OBS's window; No keeps OBS running |
| OBS did not start recording | OBS shows the reason in its own window, not to the app. Read it there; the game stays armed |
| No text source is connected | The recording carries on without lines until a hooker connects. Start the hooker, or check its port in **Settings…** |
| OBS lost its connection while recording | Lines keep being recorded; the app catches up when OBS is back |
| OBS closed during the recording | The session is saved up to that moment |
| OBS split the recording into a second file | Lines after the split get no subtitle. The app's profile has splitting off; do not turn it on |
| No lines were recorded | The video is kept without a subtitle |
| The video is still in use, so the session could not be moved | The app tries again at the next launch. The row reads "Not moved yet; retried at next launch" |
| The output folder cannot be written to | Choose another **Output folder** in **Settings…** |
| Only a few GB free | See section 11 |
| The text feed is off for this run: port in use | Another program uses the port. Change **Page port** or **WebSocket port** in **Settings…** |
| OCR stopped after several exits in a row | The recording goes on without OCR. The banner quotes owocr's own error; fix the area or engine in the game's profile |
| The app crashed, or the PC lost power | Nothing to do: at the next launch the app switches OBS back to your profile and scene collection and finishes any interrupted session |
| Subtitles may be off by a few seconds | The app restarted during a recording and could not time the rest of it exactly. Only that session is affected |
| OBS stayed on the app's profile after the app closed | Start the app again, or pick your profile and scene collection in OBS's Profile and Scene Collection menus |
| A session in Anki Miner shows the wrong episode number | Check the game folder holds nothing but the app's own files |
