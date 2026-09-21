# Wave 1 integration: spec amendments for the M0 gate

Spec readings and departures that came out of the wave 1 integration review
(`.orchestration/reviews/wave-1-cross-*.md`) and its fix round. The code already follows each one;
the M0 amender writes them into `docs/specs/2026-09-20-anki-miner-game-design.md` beside the
`docs/m0/*.md` findings. Section numbers are the spec's.

## Text intake

1. **8.1, frame parse.** "take `sentence` and `source`" becomes "take `sentence`; `source` and
   `time` are ignored: a line belongs to the configured source, and its time is read at receipt".
   `LineSink` has no slot for `source`, and using it as `source_id` would put producer labels such
   as `GSM` into `sources_used` and break `GameProfile.source_ids`
   (`anki_miner_game/text/sources/websocket_source.py`, module header).
2. **8.2 step 2.** "(`Cc`, `Cf` categories)" becomes "(`Cc`, `Cf`) and lone surrogates (`Cs`)".
   `json.loads` turns an unpaired `\udXXX` escape from a UTF-16 pair cut in half into a lone
   surrogate, which the journal, the subtitle and the feed cannot encode
   (`anki_miner_game/text/pipeline.py`, `_normalise`).
3. **8.2 steps 8 and 9.** "The previous accepted line" means the previous line journalled in the
   current recording. The actor calls `TextPipeline.reset()` when the line it accepted last will not
   be journalled: after dropping an accepted line as `paused`, at `STARTED` unless the auto-start
   line held while armed is journalled at offset 0, and after a split stop. A typewriter merge is
   journalled as a `replace` record only when its base line is the journal's last `line` record;
   otherwise it is journalled as a new line at `offset_ms(t_mono)` (dropped as `paused` when that is
   `None`). A new session's first line is therefore never a duplicate of the last one.
4. **8.1 `WebsocketSource`.** The client closes with `close_timeout=1.0` besides
   `ping_interval=None`: hookers answer neither pings nor close frames.

## Cues, clock and finalise

5. **9, withdrawn.** T02's proposed amendment ("`D` for a line whose next line starts after
   `stop_ms` is measured to the stop") is withdrawn. Finalise ignores every journal record after the
   first `stop` record (item 6), so no line reaches `build_cues` past the stop; the clause stays in
   the code as a guard only and changes no result.
6. **7 / 10.3 step 1, split.** With several `stop` records the first wins and every record after it
   is ignored: its lines are neither cues nor `skip`, and a `replace` record there rewrites nothing.
   `skip` keeps its spec 5 meaning (skip-mode click-through only).
7. **7, readings.** A stop reading or drift sample is never below the latest line offset, but it
   does not move the offset clamp: only line offsets are non-decreasing. An `outputDuration` anchor
   whose round-trip midpoint falls before the latest pause edge is carried forward by the time the
   recording ran in between.
8. **10.3, concurrency.** Finalise calls under one output root run on one worker, one at a time
   (a single-thread executor or a lock), never on a shared pool: the NN bump is check-then-act, and
   two sessions of one game bumped to the same NN would both move onto one video.

## OBS

9. **3.3 / 11.1 / 11.2, readiness.** OBS is ready when `GetVersion` succeeds, not when the
   websocket accepts a connection: until OBS has loaded, and during a scene collection change,
   every request gets 207 `NotReady` (`source-findings.md` summary items 5 and 11, sections 5 and
   8). The gateway retries 207 until a timeout, then raises `ObsRequestError`. The Protocol
   docstrings follow once the orchestrator accepts the contract change request filed with this
   round.
10. **12, window closed.** Replace "the pinned window string missing twice" with S1's rule
    (`source-findings.md` summary item 12, section 10): only items with `itemEnabled: true` count,
    because OBS keeps listing the configured value as a disabled item after the window closes or is
    retitled.
    Windows: open while an enabled item has the class and exe of `capture.window` (decode `#3A` and
    `#22`, compare case-insensitively), queried on the `game_capture` input, whose list keeps
    minimized windows. X11: open while item 0 is enabled or an enabled item has the stored xid.
    Needs `WindowItem.enabled` (contract change request filed with this round).
11. **11.3, property names.** `propertyName` is `window` for `game_capture`, `window_capture` and
    `wasapi_process_output_capture` (value `<title>:<class>:<exe>`) and `capture_window` for
    `xcomposite_input` (value `<xid>\r\n<name>\r\n<class>`). The window picker offers enabled items
    only.

## VAD

12. **13.1, where uv puts things.** Covered by `owocr.md` amendment 2, now extended to 13.1: every
    uv call of either add-on runs with `addons.bootstrap.uv_environment(home, addon)`.
13. **13.2, worker protocol.** `total_ms` is `null` when the file states no duration (a recording
    cut off by a crash; spec 6.4 keeps those). `--track` counts audio tracks only. Regions are on
    the file's timeline: a track that starts after the file shifts them by its start offset, and
    audio the demuxer lost is replaced by silence so later regions keep their place
    (`anki_miner_game/vad/worker/vad_worker.py`, module header).
14. **13.3, assignment.**
    - Windows are half-open: a region that begins exactly at the next cue's live start belongs to
      the next cue.
    - OCR mode keeps step 1's skip rule for a cue that finds no snap region.
    - The snap interval is `[max(prev.live_end, start - SNAP_LOOKBACK_MS), start]`, and the first
      cue's interval opens at `max(0, start - SNAP_LOOKBACK_MS)`. `SNAP_LOOKBACK_MS` is 10 s,
      provisional until T32.
    - A cue whose chain is empty keeps its live start and end, except when the next cue's start
      snapped back: its live end then gets the step 3 clamp (`next.final_start - end_gap_ms`, with
      the `MIN_CUE_MS` floor). Hook mode is unchanged.
