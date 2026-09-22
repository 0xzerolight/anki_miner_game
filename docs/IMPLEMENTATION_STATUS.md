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
| M0 | spikes against a real OBS: sync probe and clock, profile switching, settings, audio capture string, rename lock, hotkey, owocr, Wayland clipboard, discovery, disk rate | merged on `integration/wave-2b` (Linux; Windows rows wait for H5): S1 `e4dea35`, R3 `af290d5`, S2 `8b8551e`, R1 `e53e3a8`, R2 `ed1e7bf`, gate `98f8294`, contracts `4bd12e7`, provisioning `7e1e054` |
| M1 | core loop on the app's own OBS profile | merged on `integration/wave-2b`; Linux exit passed (E1 `37c4a7e`), Windows exit at H5: T00 `4458ca6`, T01 `0fb6739`, W0 hardening `a8c2072`, T02 `b0b753b`, T03 `226569d`, T04 `effe532`, T05 `cf4cb71`, T06 `622c09e`, T07 `170c2d1`, T12 `5aec2a0`, T15 `30ea5e3`, T16 `edc14b4` |
| M2 | provisioning, game profiles, wizard, tray, hotkey and CLI verbs, auto mode, text feed | in progress: T08 `f90d72f`, T13 `7c80ef8`, T14 `6d3b8de`, T17 `7323f07`, T18 `f1a864a`, T22 `6c06266`, T20 `098b6a8`, T21 `52cea41` merged; T19 main window and T26 wiring (`feat/t26-wiring`) and T25 integration tests not yet merged |
| M3 | add-on bootstrap and VAD pass | in progress: T09 `0f27547`, T10 `3b6166f`, T11 `1ccd786`, T23 `875ae68` merged; T31 threshold tuning waits for H3 recordings |
| M4 | OCR add-on | in progress: T24 `63a4141` (contracts `fb8d302`) merged; T32 tuning waits for H4 sessions |
| M5 | packaging, CI, release, user guide | in progress: T27 PyInstaller, T28 installers, T29 CI and release, T30 user guide on their branches; release dry-run and H5 QA (`docs/qa/h5-checklist.md`) to follow |

Merge commits are on `integration/wave-2b` (`git log --first-parent`); `main` stops at `052cc99`
(wave 2a) and takes the rest when the owner merges it.

Next step: wave 4 (T27-T30) and the release dry-run, then the human gates H3 (VAD recordings), H4
(OCR sessions) and H5 (release QA and every Windows check). See
`docs/plans/2026-09-21-master-plan.md` for the full task graph.

Open items carried from the design (spec Appendix B): M0 settled them on Linux or in source. Still
open until H5: the OBS registry key on a real install, the application-audio string at runtime, the
Windows zero event and latency with `game_capture`, the job-object kill of owocr, rename-lock
timing, `RegisterHotKey` under a game, and `xcomposite_input` capture on a real X11 desktop.
