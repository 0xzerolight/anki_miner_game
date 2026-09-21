"""Anki Miner hand-off probe (E1; spec 3.1, 18.4, Appendix A): does Anki Miner take the app's pair?

Given one game folder holding at least three finalised sessions (``<Title> - NN.mkv``, ``.srt`` and
``.session.json``), it asks Anki Miner's own code what its GUI would do:

- ``same_stem_autofill``: Video -> Single auto-fills the same-stem ``.srt`` for every video
  (``find_sibling_subtitle``, what ``single_episode_tab.py`` calls when a video is picked); the
  ``.session.json`` sidecar is never offered.
- ``batch_pairing``: Video -> Batch over the folder, as both video and subtitle folder, pairs
  every session with its own subtitle by episode number (``find_pairs_by_episode_number``).
- ``subtitle_parse``: Anki Miner's parser reads every cue of every ``.srt`` back with the app's
  timings and text (``parse_raw_entries``).
- ``subtitle_words``: its tokenizer mines words from the cues, each carrying its line's timing
  (``parse_subtitle_file``).
- ``audio_clips``: its media extractor cuts the audio clip (and screenshot) of ``--clips`` cues,
  one per session in turn, each as long as the cue plus ``audio_padding`` on both sides.

Run it with Anki Miner's interpreter from this repository's root, read-only, with a private home
(it refuses to run otherwise, and never writes ``.pyc`` files into Anki Miner's checkout)::

    env HOME=$P/home ANKI_MINER_HOME=$P/amhome TMPDIR=$P/tmp \\
      /home/light/Projects/anki_miner/.venv/bin/python -B -m tools.handoff_probe \\
      --game-dir "$ROOT/Title" --work $P/work [--clips 3] [--json REPORT.json]

Exit 0 when every check passes, 1 when one fails, 2 when it refuses to run.
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True  # before Anki Miner is imported: its checkout stays as it is

import argparse  # noqa: E402 - after the bytecode switch on purpose
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import subprocess  # noqa: E402
from collections.abc import Callable, Mapping, Sequence  # noqa: E402
from dataclasses import asdict, dataclass  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Protocol  # noqa: E402

MIN_SESSIONS = 3
CLIP_TOLERANCE_S = 0.1
"""An mp3 clip may run a frame or two (26 ms each) past the window it was cut to."""
TIME_TOLERANCE_S = 0.0015
"""Anki Miner reads times as float seconds; the app writes whole milliseconds."""
CHECKS = ("sessions", "same_stem_autofill", "batch_pairing", "subtitle_parse", "subtitle_words", "audio_clips")
EXIT_FAILED = 1
EXIT_REFUSED = 2

_TIMING = re.compile(r"^(\d{2,}):(\d{2}):(\d{2}),(\d{3}) --> (\d{2,}):(\d{2}):(\d{2}),(\d{3})$")


@dataclass(frozen=True)
class SrtCue:
    index: int
    start_ms: int
    end_ms: int
    text: str


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


class Word(Protocol):
    """What the probe reads of Anki Miner's ``TokenizedWord``."""

    @property
    def mined_form(self) -> str: ...

    @property
    def sentence(self) -> str: ...

    @property
    def start_time(self) -> float: ...

    @property
    def end_time(self) -> float: ...


class AnkiMiner(Protocol):
    """The Anki Miner calls the probe makes; ``load_anki_miner`` builds the real one."""

    padding: float

    def sibling_subtitle(self, video: Path) -> Path | None: ...

    def batch_pairs(self, folder: Path) -> list[tuple[Path, Path]]: ...

    def raw_entries(self, subtitle: Path) -> list[tuple[float, float, str]]: ...

    def words(self, subtitle: Path) -> Sequence[Word]: ...

    def extract(self, video: Path, word: Word, out: Path) -> tuple[Path | None, Path | None]:
        """``(audio, screenshot)``; ``None`` for one Anki Miner could not cut."""
        ...


def _ms(hours: str, minutes: str, seconds: str, millis: str) -> int:
    return ((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 1000 + int(millis)


def read_srt(text: str) -> list[SrtCue]:
    """The cues of a ``.srt`` in the app's own format (spec 9): number, timing, one text line."""
    cues: list[SrtCue] = []
    for number, block in enumerate((b for b in text.split("\n\n") if b.strip()), start=1):
        lines = block.strip("\n").split("\n")
        match = _TIMING.match(lines[1]) if len(lines) >= 2 else None
        if match is None or not lines[0].isdigit():
            raise ValueError(f"block {number} is not an SRT cue: {block!r}")
        g = match.groups()
        cues.append(SrtCue(int(lines[0]), _ms(*g[:4]), _ms(*g[4:]), "\n".join(lines[2:])))
    return cues


def expected_clip_s(start_s: float, end_s: float, padding_s: float) -> float:
    """Length of Anki Miner's clip for a line: ``padding`` either side, the start clamped at 0
    (``media_extractor.resolve_audio_window``)."""
    return end_s + padding_s - max(0.0, start_s - padding_s)


def isolation_problems(env: Mapping[str, str], owner_home: Path) -> list[str]:
    """Why Anki Miner must not run in ``env``: it would read or write the owner's files."""
    problems: list[str] = []
    home = env.get("HOME")
    if home and Path(home) == owner_home:
        problems.append(f"HOME is the real home {owner_home}")
    am_home = env.get("ANKI_MINER_HOME")
    if not am_home:
        problems.append("ANKI_MINER_HOME is not set")
    elif Path(am_home) == owner_home / ".anki_miner":
        problems.append(f"ANKI_MINER_HOME is the real Anki Miner home {owner_home / '.anki_miner'}")
    return problems


def media_duration_s(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(out.stdout.strip())


def session_videos(game_dir: Path) -> list[Path]:
    return sorted(p for p in game_dir.iterdir() if p.is_file() and p.suffix == ".mkv")


# --- the checks ------------------------------------------------------------------------------------


def run(
    game_dir: Path,
    work: Path,
    am: AnkiMiner,
    *,
    clips: int = 3,
    duration: Callable[[Path], float] = media_duration_s,
) -> list[Check]:
    videos = session_videos(game_dir)
    subtitles = {video: video.with_suffix(".srt") for video in videos}
    cues = {video: read_srt(srt.read_text(encoding="utf-8")) for video, srt in subtitles.items() if srt.exists()}
    checks = [
        Check("sessions", len(videos) >= MIN_SESSIONS, f"{len(videos)} sessions: {[v.name for v in videos]}"),
        _same_stem(am, subtitles),
        _batch(am, game_dir, subtitles),
        _parse(am, subtitles, cues),
    ]
    words = {video: list(am.words(subtitles[video])) for video in cues}
    checks.append(_words(words, cues))
    checks.append(_clips(am, work, words, cues, clips, duration))
    return checks


def _same_stem(am: AnkiMiner, subtitles: Mapping[Path, Path]) -> Check:
    wrong = {v.name: str(got) for v, srt in subtitles.items() if (got := am.sibling_subtitle(v)) != srt}
    return Check("same_stem_autofill", bool(subtitles) and not wrong, f"wrong or missing: {wrong}" if wrong else "ok")


def _batch(am: AnkiMiner, game_dir: Path, subtitles: Mapping[Path, Path]) -> Check:
    got = set(am.batch_pairs(game_dir))
    want = set(subtitles.items())
    detail = f"{len(got)} pairs" if got == want else f"got {sorted(got)}, want {sorted(want)}"
    return Check("batch_pairing", bool(want) and got == want, detail)


def _same_time(a: float, b: float) -> bool:
    return abs(a - b) <= TIME_TOLERANCE_S


def _parse(am: AnkiMiner, subtitles: Mapping[Path, Path], cues: Mapping[Path, list[SrtCue]]) -> Check:
    problems: list[str] = []
    total = 0
    for video, want in cues.items():
        got = am.raw_entries(subtitles[video])
        total += len(got)
        same = len(got) == len(want) and all(
            _same_time(s, c.start_ms / 1000) and _same_time(e, c.end_ms / 1000) and t == c.text
            for (s, e, t), c in zip(got, want, strict=True)
        )
        if not same:
            problems.append(f"{video.name}: {len(got)} entries against {len(want)} cues")
    ok = bool(cues) and len(cues) == len(subtitles) and not problems
    return Check("subtitle_parse", ok, "; ".join(problems) or f"{total} cues read back")


def _cue_of(word: Word, cues: Sequence[SrtCue]) -> SrtCue | None:
    return next(
        (
            c
            for c in cues
            if _same_time(word.start_time, c.start_ms / 1000) and _same_time(word.end_time, c.end_ms / 1000)
        ),
        None,
    )


def _words(words: Mapping[Path, Sequence[Word]], cues: Mapping[Path, list[SrtCue]]) -> Check:
    total = sum(len(w) for w in words.values())
    stray = [f"{v.name}: {w.mined_form}" for v, ws in words.items() for w in ws if _cue_of(w, cues[v]) is None]
    detail = f"{total} words" + (f"; off their line: {stray}" if stray else "")
    return Check("subtitle_words", total > 0 and not stray, detail)


def _clip_candidates(
    words: Mapping[Path, Sequence[Word]], cues: Mapping[Path, list[SrtCue]], lengths: Mapping[Path, float], pad: float
) -> list[tuple[Path, Word, SrtCue]]:
    """One word per cue, sessions taken in turn, leaving out cues whose padded end runs past the video."""
    per_session: list[list[tuple[Path, Word, SrtCue]]] = []
    for video, ws in words.items():
        chosen: dict[int, tuple[Path, Word, SrtCue]] = {}
        for word in ws:
            cue = _cue_of(word, cues[video])
            if cue is not None and cue.index not in chosen and cue.end_ms / 1000 + pad <= lengths[video]:
                chosen[cue.index] = (video, word, cue)
        per_session.append([chosen[i] for i in sorted(chosen)])
    ordered: list[tuple[Path, Word, SrtCue]] = []
    for depth in range(max((len(s) for s in per_session), default=0)):
        ordered += [s[depth] for s in per_session if depth < len(s)]
    return ordered


def _clips(
    am: AnkiMiner,
    work: Path,
    words: Mapping[Path, Sequence[Word]],
    cues: Mapping[Path, list[SrtCue]],
    clips: int,
    duration: Callable[[Path], float],
) -> Check:
    lengths = {video: duration(video) for video in words}
    chosen = _clip_candidates(words, cues, lengths, am.padding)[:clips]
    problems: list[str] = []
    done: list[str] = []
    for video, word, cue in chosen:
        audio, screenshot = am.extract(video, word, work / "clips")
        want = expected_clip_s(cue.start_ms / 1000, cue.end_ms / 1000, am.padding)
        if audio is None or not audio.exists():
            problems.append(f"{video.name} cue {cue.index}: no audio clip")
            continue
        got = duration(audio)
        if abs(got - want) > CLIP_TOLERANCE_S:
            problems.append(f"{video.name} cue {cue.index}: clip {got:.3f} s, want {want:.3f} s")
        if screenshot is None or not screenshot.exists():
            problems.append(f"{video.name} cue {cue.index}: no screenshot")
        done.append(f"{video.name} cue {cue.index} {word.mined_form!r}: {got:.3f} s (want {want:.3f} s)")
    if len(chosen) < clips:
        problems.append(f"only {len(chosen)} cues can be clipped, {clips} wanted")
    return Check("audio_clips", not problems, "; ".join(problems + done))


# --- Anki Miner itself -----------------------------------------------------------------------------


class _RealAnkiMiner:
    """Anki Miner's own services, built the way its GUI builds them for Japanese (config defaults)."""

    def __init__(self, work: Path) -> None:
        from anki_miner.config.config import AnkiMinerConfig
        from anki_miner.services.media_extractor import MediaExtractorService
        from anki_miner.services.subtitle_parser import SubtitleParserService
        from anki_miner.utils import file_pairing

        self.config = AnkiMinerConfig(media_temp_folder=work / "media_temp")
        self.padding = float(self.config.audio_padding)
        self._pairing = file_pairing
        self._parser = SubtitleParserService(self.config)
        self._media = MediaExtractorService(self.config)

    def sibling_subtitle(self, video: Path) -> Path | None:
        found: Path | None = self._pairing.find_sibling_subtitle(video)
        return found

    def batch_pairs(self, folder: Path) -> list[tuple[Path, Path]]:
        pairs = self._pairing.FilePairMatcher.find_pairs_by_episode_number(folder, folder)
        return [(pair.video, pair.subtitle) for pair in pairs]

    def raw_entries(self, subtitle: Path) -> list[tuple[float, float, str]]:
        return list(self._parser.parse_raw_entries(subtitle))

    def words(self, subtitle: Path) -> Sequence[Word]:
        return list(self._parser.parse_subtitle_file(subtitle))

    def extract(self, video: Path, word: Word, out: Path) -> tuple[Path | None, Path | None]:
        out.mkdir(parents=True, exist_ok=True)
        media = self._media.extract_media(video, word, temp_folder=out)
        return media.audio_path, media.screenshot_path


def _anki_miner_revision() -> dict[str, str]:
    import anki_miner

    root = Path(anki_miner.__file__).resolve().parents[1]
    head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    return {"anki_miner": str(root), "commit": head.stdout.strip()}


def _owner_home() -> Path:
    import pwd  # POSIX: the probe runs on the Linux host only

    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="handoff_probe", description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--game-dir", type=Path, required=True, help="the game folder with the sessions")
    parser.add_argument("--work", type=Path, required=True, help="private folder for clips and Anki Miner's temp files")
    parser.add_argument("--clips", type=int, default=3, help="how many cues to cut a clip for")
    parser.add_argument("--json", type=Path, help="also write the report here")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    problems = isolation_problems(os.environ, _owner_home())
    if problems:
        for problem in problems:
            print(f"handoff_probe: refusing to run: {problem}", file=sys.stderr)
        return EXIT_REFUSED
    args.work.mkdir(parents=True, exist_ok=True)
    checks = run(args.game_dir, args.work, _RealAnkiMiner(args.work), clips=args.clips)
    for check in checks:
        print(f"{'PASS' if check.passed else 'FAIL'} {check.name}: {check.detail}")
    passed = all(check.passed for check in checks)
    print("PASS" if passed else "FAIL")
    if args.json:
        report: dict[str, Any] = {
            "game_dir": str(args.game_dir),
            "passed": passed,
            **_anki_miner_revision(),
            "checks": [asdict(check) for check in checks],
        }
        args.json.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0 if passed else EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
