"""Sync-probe analyser (spec 18.3): first white frame per flash against zero events or cue starts.

``--raw`` (M0 spike R1, no app involved): for each candidate zero event in the OBS transcript
(``StartRecord`` sent, its response, ``STARTING``, ``STARTED``) predict where each flash should land
in the recording, ``(t_flash - zero - paused_before) * 1000``, and compare with the first white
frame found near it. The median error is that candidate's ``capture_latency_ms``; the residuals
around it show whether one constant fits every flash::

    python -m tools.sync_probe.analyse --raw --recording REC.mkv \\
        --flasher-log FLASHER.jsonl --obs-transcript TRANSCRIPT.jsonl [--json REPORT.json]

``--app`` (E1 and every release): compare each flash with the arrival of the matching line in the
app's ``.srt``: its cue start less the session's start shift, ``START_SHIFT_MS`` of the manifest's
``text_mode`` (the ``.session.json`` beside the subtitle unless ``--manifest`` names one). Pass when
every flash has a cue and every cue a flash, each within 150 ms::

    python -m tools.sync_probe.analyse --app --recording REC.mkv --srt REC.srt [--manifest REC.session.json]

Inputs come from ``tools.sync_probe.flasher`` (flash ``t_mono`` values) and
``tools.obs_transcript_recorder`` (OBS frames with ``t_mono``). All ``t_mono`` values are
``time.monotonic()`` on the same host, so they compare directly.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from anki_miner_game.models.constants import START_SHIFT_MS
from anki_miner_game.session.manifest import MANIFEST_SUFFIX, load_manifest
from anki_miner_game.store import StoreError
from tools.sync_probe import video

PASS_BOUND_MS = 150
MATCH_WINDOW_MS = 1000
_STATE_PREFIX = "OBS_WEBSOCKET_OUTPUT_"


class AnalysisError(Exception):
    """The inputs do not describe a recording the probe can analyse."""


@dataclass(frozen=True)
class FlashEvent:
    index: int
    t_mono: float
    text: str


@dataclass(frozen=True)
class ObsTimeline:
    candidates: dict[str, float]  # candidate zero event -> t_mono
    pauses: list[tuple[float, float | None]]  # (PAUSED, RESUMED or None if never resumed)
    end: float | None  # STOPPING (or STOPPED) t_mono; flashes after it are not in the file


@dataclass(frozen=True)
class Match:
    flash_index: int
    predicted_ms: int
    measured_ms: int

    @property
    def error_ms(self) -> int:
        return self.measured_ms - self.predicted_ms


@dataclass(frozen=True)
class CandidateReport:
    zero_t_mono: float
    matches: list[Match]
    unmatched: list[int]  # flashes recorded but with no white frame near the prediction
    not_recorded: list[int]  # flashes before the zero, inside a pause or after the end

    @property
    def errors_ms(self) -> list[int]:
        return [m.error_ms for m in self.matches]

    @property
    def median_ms(self) -> float | None:
        return statistics.median(self.errors_ms) if self.matches else None

    @property
    def spread_ms(self) -> int | None:
        return max(self.errors_ms) - min(self.errors_ms) if self.matches else None

    @property
    def max_abs_residual_ms(self) -> float | None:
        median = self.median_ms
        return None if median is None else max(abs(e - median) for e in self.errors_ms)

    def to_dict(self) -> dict[str, Any]:
        errors = self.errors_ms
        return {
            "zero_t_mono": self.zero_t_mono,
            "n": len(errors),
            "errors_ms": errors,
            "median_ms": self.median_ms,
            "mean_ms": statistics.fmean(errors) if errors else None,
            "min_ms": min(errors, default=None),
            "max_ms": max(errors, default=None),
            "spread_ms": self.spread_ms,
            "max_abs_residual_ms": self.max_abs_residual_ms,
            "matches": [asdict(m) for m in self.matches],
            "unmatched": self.unmatched,
            "not_recorded": self.not_recorded,
        }


@dataclass(frozen=True)
class Pair:
    detection_ms: int
    cue_ms: int
    text: str
    shift_ms: int = 0
    """The session's start shift: the cue starts this far from its line's arrival."""

    @property
    def arrival_ms(self) -> int:
        return self.cue_ms - self.shift_ms

    @property
    def diff_ms(self) -> int:
        return self.arrival_ms - self.detection_ms


@dataclass(frozen=True)
class AppReport:
    pairs: list[Pair]
    unmatched_flashes: list[int] = field(default_factory=list)  # detection times with no cue
    unmatched_cues: list[tuple[int, str]] = field(default_factory=list)
    bound_ms: int = PASS_BOUND_MS

    @property
    def passed(self) -> bool:
        return (
            not self.unmatched_flashes
            and not self.unmatched_cues
            and all(abs(p.diff_ms) < self.bound_ms for p in self.pairs)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "bound_ms": self.bound_ms,
            "pairs": [{**asdict(p), "arrival_ms": p.arrival_ms, "diff_ms": p.diff_ms} for p in self.pairs],
            "unmatched_flashes": self.unmatched_flashes,
            "unmatched_cues": self.unmatched_cues,
        }


# --- inputs --------------------------------------------------------------------------------------


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_flasher_log(path: Path) -> list[FlashEvent]:
    return [FlashEvent(r["index"], r["t_mono"], r["text"]) for r in _jsonl(path) if r.get("kind") == "flash"]


def _message(record: dict[str, Any]) -> dict[str, Any]:
    msg = record.get("msg")
    return msg if isinstance(msg, dict) else {}


def _is_start_request(record: dict[str, Any]) -> bool:
    msg = _message(record)
    return record.get("dir") == "client->obs" and msg.get("op") == 6 and msg["d"].get("requestType") == "StartRecord"


def _record_state(record: dict[str, Any]) -> str | None:
    msg = _message(record)
    if record.get("dir") != "obs->client" or msg.get("op") != 5:
        return None
    if msg["d"].get("eventType") != "RecordStateChanged":
        return None
    state = str(msg["d"].get("eventData", {}).get("outputState", ""))
    return state.removeprefix(_STATE_PREFIX)


def load_obs_timeline(path: Path, session: int = 0) -> ObsTimeline:
    """Candidate zero events, pauses and end of the ``session``-th recording in a transcript."""
    records = sorted(_jsonl(path), key=lambda r: r["t_mono"])
    starts = [i for i, r in enumerate(records) if _is_start_request(r)]
    if len(starts) <= session:
        raise AnalysisError(f"{path}: no StartRecord request #{session} (found {len(starts)})")
    window = records[starts[session] : starts[session + 1] if session + 1 < len(starts) else len(records)]
    candidates = {"request": float(window[0]["t_mono"])}
    pauses: list[tuple[float, float | None]] = []
    paused_at: float | None = None
    end: float | None = None
    for record in window[1:]:
        t = float(record["t_mono"])
        msg = _message(record)
        if record.get("dir") == "obs->client" and msg.get("op") == 7 and msg["d"].get("requestType") == "StartRecord":
            candidates.setdefault("response", t)
        state = _record_state(record)
        if state == "STARTING":
            candidates.setdefault("starting", t)
        elif state == "STARTED":
            candidates.setdefault("started", t)
        elif state == "PAUSED" and paused_at is None:
            paused_at = t
        elif state == "RESUMED" and paused_at is not None:
            pauses.append((paused_at, t))
            paused_at = None
        elif state in ("STOPPING", "STOPPED"):
            end = t
            break
    if paused_at is not None:
        pauses.append((paused_at, None))
    order = ("request", "starting", "response", "started")
    return ObsTimeline({k: candidates[k] for k in sorted(candidates, key=order.index)}, pauses, end)


def load_shift_ms(manifest: Path) -> int:
    """The start shift the session's cues were built with: ``START_SHIFT_MS`` of its ``text_mode``."""
    try:
        return START_SHIFT_MS[load_manifest(manifest).text_mode]
    except FileNotFoundError:
        raise AnalysisError(f"{manifest}: no session manifest (pass --manifest)") from None


def load_cues(path: Path) -> list[tuple[int, str]]:
    import pysubs2  # dev dependency; the analyser is a dev tool

    subs = pysubs2.load(str(path), encoding="utf-8")
    return [(int(event.start), event.plaintext) for event in subs]


# --- analysis --------------------------------------------------------------------------------------


def paused_ms(t_mono: float, pauses: Sequence[tuple[float, float | None]]) -> float | None:
    """Milliseconds paused before ``t_mono``; None while paused (nothing is recorded then)."""
    total = 0.0
    for start, end in pauses:
        if t_mono < start:
            continue
        if end is None or t_mono < end:
            return None
        total += (end - start) * 1000
    return total


def _nearest(target: float, pool: list[int], window_ms: float) -> int | None:
    best = min(pool, key=lambda d: abs(d - target), default=None)
    return best if best is not None and abs(best - target) <= window_ms else None


def analyse_raw(
    flashes: Sequence[FlashEvent],
    timeline: ObsTimeline,
    detections_ms: Sequence[int],
    window_ms: float = MATCH_WINDOW_MS,
) -> dict[str, CandidateReport]:
    reports = {}
    for name, zero in timeline.candidates.items():
        pool = list(detections_ms)
        matches, unmatched, not_recorded = [], [], []
        for flash in sorted(flashes, key=lambda f: f.t_mono):
            paused = paused_ms(flash.t_mono, timeline.pauses)
            if flash.t_mono < zero or paused is None or (timeline.end is not None and flash.t_mono >= timeline.end):
                not_recorded.append(flash.index)
                continue
            predicted = round((flash.t_mono - zero) * 1000 - paused)
            found = _nearest(predicted, pool, window_ms)
            if found is None:
                unmatched.append(flash.index)
                continue
            pool.remove(found)
            matches.append(Match(flash.index, predicted, found))
        reports[name] = CandidateReport(zero, matches, unmatched, not_recorded)
    return reports


def analyse_app(
    cues: Sequence[tuple[int, str]],
    detections_ms: Sequence[int],
    *,
    shift_ms: int = 0,
    bound_ms: int = PASS_BOUND_MS,
    window_ms: float = MATCH_WINDOW_MS,
) -> AppReport:
    """Pair each flash with the nearest line arrival, ``cue start - shift_ms``.

    ``shift_ms`` is the session's start shift, so the probe measures the app's own timing whatever
    the shift. A cue clamped at 0 (a line in the recording's first ``-shift_ms``) reads as late.
    """
    texts = dict(cues)
    pool = [start - shift_ms for start, _ in cues]
    pairs, unmatched = [], []
    for detection in detections_ms:
        found = _nearest(detection, pool, window_ms)
        if found is None:
            unmatched.append(detection)
            continue
        pool.remove(found)
        pairs.append(Pair(detection, found + shift_ms, texts[found + shift_ms], shift_ms))
    left = [(start, text) for start, text in cues if start - shift_ms in pool]
    return AppReport(pairs, unmatched, left, bound_ms)


# --- CLI -------------------------------------------------------------------------------------------


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="analyse", description=(__doc__ or "").split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--raw", action="store_true", help="compare flashes with the candidate zero events")
    mode.add_argument("--app", action="store_true", help="compare flashes with the app's cue starts")
    parser.add_argument("--recording", type=Path, required=True)
    parser.add_argument("--flasher-log", type=Path, help="--raw: the flasher's JSONL log")
    parser.add_argument("--obs-transcript", type=Path, help="--raw: the transcript recorder's JSONL")
    parser.add_argument("--session", type=int, default=0, help="--raw: which StartRecord in the transcript")
    parser.add_argument("--srt", type=Path, help="--app: the app's subtitle file")
    parser.add_argument("--manifest", type=Path, help="--app: the session's manifest (default: beside --srt)")
    parser.add_argument("--threshold", type=float, help="luma threshold (default: midway, per recording)")
    parser.add_argument("--window-ms", type=float, default=MATCH_WINDOW_MS, help="max distance to pair")
    parser.add_argument("--bound-ms", type=int, default=PASS_BOUND_MS, help="--app pass bound")
    parser.add_argument("--json", type=Path, help="also write the full report here")
    args = parser.parse_args(argv)
    if args.raw and (args.flasher_log is None or args.obs_transcript is None):
        parser.error("--raw needs --flasher-log and --obs-transcript")
    if args.app and args.srt is None:
        parser.error("--app needs --srt")
    return args


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse(argv)
    try:
        shift_ms = load_shift_ms(args.manifest or args.srt.with_suffix(MANIFEST_SUFFIX)) if args.app else 0
        detection = video.detect_flashes(video.frame_lumas(args.recording), args.threshold)
        detections = [f.t_ms for f in detection.flashes]
        report: dict[str, Any] = {
            "recording": str(args.recording),
            "threshold": detection.threshold,
            "starts_bright": detection.starts_bright,
            "detections_ms": detections,
        }
        if detection.starts_bright:
            print("warning: the recording starts on a white frame; that flash is not counted", file=sys.stderr)
        if args.raw:
            flashes = load_flasher_log(args.flasher_log)
            timeline = load_obs_timeline(args.obs_transcript, args.session)
            results = analyse_raw(flashes, timeline, detections, args.window_ms)
            report |= {"mode": "raw", "pauses": timeline.pauses, "end_t_mono": timeline.end}
            report["candidates"] = {name: r.to_dict() for name, r in results.items()}
            print(f"{len(detections)} white-frame onsets, {len(flashes)} flashes logged")
            print(f"{'candidate':<10} {'n':>3} {'median':>8} {'min':>6} {'max':>6} {'spread':>7} {'max|res|':>9}")
            for name, r in results.items():
                row = r.to_dict()
                print(
                    f"{name:<10} {row['n']:>3} {_fmt(row['median_ms']):>8} {_fmt(row['min_ms']):>6} "
                    f"{_fmt(row['max_ms']):>6} {_fmt(row['spread_ms']):>7} {_fmt(row['max_abs_residual_ms']):>9}"
                    + (f"  unmatched {row['unmatched']}" if row["unmatched"] else "")
                )
            rc = 0
        else:
            cues = load_cues(args.srt)
            app = analyse_app(cues, detections, shift_ms=shift_ms, bound_ms=args.bound_ms, window_ms=args.window_ms)
            report |= {"mode": "app", "shift_ms": shift_ms, **app.to_dict()}
            print(f"start shift {shift_ms} ms: each flash is compared with its cue start - shift")
            for pair in app.pairs:
                print(
                    f"flash {pair.detection_ms:>8} ms  cue {pair.cue_ms:>8} ms  line {pair.arrival_ms:>8} ms  "
                    f"diff {pair.diff_ms:>+5} ms  {pair.text}"
                )
            for detection_ms in app.unmatched_flashes:
                print(f"flash {detection_ms:>8} ms  no cue")
            for cue_ms, text in app.unmatched_cues:
                print(f"cue   {cue_ms:>8} ms  no flash  {text}")
            verdict = "PASS" if app.passed else "FAIL"
            print(f"{verdict}: |cue start - shift - flash| < {args.bound_ms} ms for every flash")
            rc = 0 if app.passed else 1
    except (video.VideoError, AnalysisError, StoreError, OSError, ValueError, KeyError) as exc:
        print(f"analyse: {exc}", file=sys.stderr)
        return 2
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return rc


if __name__ == "__main__":
    sys.exit(main())
