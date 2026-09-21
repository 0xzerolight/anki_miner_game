# Anki Miner Game: implementation status

Standalone game-session recorder that produces video + subtitle pairs for Anki Miner. The design is
[`specs/2026-09-20-anki-miner-game-design.md`](specs/2026-09-20-anki-miner-game-design.md); this file
only records progress.

Folder rules (same as Anki Miner's `docs/mining-languages/`):

- `specs/` holds the active design.
- `plans/` holds plans for milestones that have not shipped.
- `completed/` holds plans for milestones that have shipped. Move a plan here when it merges.

The code lives in this repository, `anki_miner_game` (`0xzerolight/anki_miner_game`, private). Nothing
in the Anki Miner repository changes for this project.

Status values: `not started` · `planned` · `in progress` · `merged` · `released`.

| Milestone | Scope | Status |
|---|---|---|
| Design | spec, two judged rounds | done 2026-09-20 |
| M0 | spikes against a real OBS: sync probe and clock, profile switching, settings, audio capture string, rename lock, hotkey, owocr, Wayland clipboard, discovery, disk rate | not started |
| M1 | core loop on the app's own OBS profile | not started |
| M2 | provisioning, game profiles, wizard, tray, hotkey and CLI verbs, auto mode, text feed | not started |
| M3 | add-on bootstrap and VAD pass | not started |
| M4 | OCR add-on | not started |
| M5 | packaging, CI, release, user guide | not started |

Next step: W0 scaffold (T00) is merging; W1 begins next (T01 contracts, then the parallel W1 task
set and the M0 spikes). See `docs/plans/2026-09-21-master-plan.md` for the full task graph.

Open items carried from the design (spec Appendix B): `basic.ini` key names for container and file
splitting, the OBS registry key, pause output-state names, `outputDuration` versus file timestamps,
which profile changes need an OBS restart, the application-audio window string, minimum OBS version.
