"""tools/handoff_probe.py: the checks run against a game folder, with Anki Miner replaced by a fake.

The real probe runs under Anki Miner's own interpreter (E1, ``docs/m0/m1-exit-linux.md``); this
project's environment has no Anki Miner, so these tests drive ``run`` through the ``AnkiMiner``
seam and check the parts that are the probe's own: the SRT reader, the expected clip length, the
isolation guard and the pass/fail logic of every check.
"""

from dataclasses import dataclass
from pathlib import Path

import pytest

from anki_miner_game.models.cue import Cue
from anki_miner_game.session.srt_writer import format_srt
from tools import handoff_probe as hp

PADDING = 0.3


@dataclass(frozen=True)
class Word:
    mined_form: str
    sentence: str
    start_time: float
    end_time: float


SESSIONS = {
    "Probe - 01": [(1000, 3000, "今日は良い天気ですね。"), (4000, 6500, "猫が窓の外を見ている。")],
    "Probe - 02": [(200, 2000, "明日は雨が降るそうです。"), (2500, 5000, "駅まで歩いて十分です。")],
    "Probe - 03": [(1500, 4000, "この本は面白かった。")],
}
VIDEO_S = 10.0


def make_game(folder: Path, sessions: dict[str, list[tuple[int, int, str]]] = SESSIONS) -> Path:
    folder.mkdir(parents=True)
    for stem, cues in sessions.items():
        (folder / f"{stem}.mkv").write_bytes(b"video")
        (folder / f"{stem}.session.json").write_text("{}", encoding="utf-8")
        srt = format_srt([Cue(i, start, end, text, "agent") for i, (start, end, text) in enumerate(cues, start=1)])
        (folder / f"{stem}.srt").write_text(srt, encoding="utf-8")
    return folder


class FakeAnkiMiner:
    """Answers the way Anki Miner does for a well-formed folder; tests break one answer at a time."""

    padding = PADDING

    def __init__(self) -> None:
        self.extracted: list[tuple[Path, Word]] = []

    def sibling_subtitle(self, video: Path) -> Path | None:
        srt = video.with_suffix(".srt")
        return srt if srt.exists() else None

    def batch_pairs(self, folder: Path) -> list[tuple[Path, Path]]:
        return [(v, v.with_suffix(".srt")) for v in sorted(folder.glob("*.mkv"))]

    def raw_entries(self, subtitle: Path) -> list[tuple[float, float, str]]:
        cues = hp.read_srt(subtitle.read_text(encoding="utf-8"))
        return [(c.start_ms / 1000, c.end_ms / 1000, c.text) for c in cues]

    def words(self, subtitle: Path) -> list[Word]:
        cues = hp.read_srt(subtitle.read_text(encoding="utf-8"))
        return [Word(c.text[:2], c.text, c.start_ms / 1000, c.end_ms / 1000) for c in cues]

    def extract(self, video: Path, word: Word, out: Path) -> tuple[Path | None, Path | None]:
        self.extracted.append((video, word))
        out.mkdir(parents=True, exist_ok=True)
        audio = out / f"{video.stem}-{word.start_time}.mp3"
        shot = out / f"{video.stem}-{word.start_time}.jpg"
        audio.write_bytes(b"mp3")
        shot.write_bytes(b"jpg")
        return audio, shot


def fake_duration(path: Path) -> float:
    if path.suffix == ".mkv":
        return VIDEO_S
    # The clip file name carries the word's start; the clip spans the padded cue.
    stem = path.stem
    start = float(stem.rsplit("-", 1)[1])
    cue = next(c for cues in SESSIONS.values() for c in cues if c[0] / 1000 == start)
    return hp.expected_clip_s(cue[0] / 1000, cue[1] / 1000, PADDING)


def by_name(checks: list[hp.Check]) -> dict[str, hp.Check]:
    return {check.name: check for check in checks}


# --- pure parts -----------------------------------------------------------------------------------


def test_read_srt_reads_back_what_the_app_writes():
    cues = [Cue(1, 0, 999, "え", "agent"), Cue(2, 3_599_999, 3_600_500, "one hour", "agent")]

    got = hp.read_srt(format_srt(cues))

    assert got == [hp.SrtCue(1, 0, 999, "え"), hp.SrtCue(2, 3_599_999, 3_600_500, "one hour")]


def test_read_srt_of_an_empty_file_is_empty():
    assert hp.read_srt("") == []


def test_read_srt_refuses_a_block_it_cannot_read():
    with pytest.raises(ValueError, match="block 1"):
        hp.read_srt("1\nnot a timing line\ntext\n")


@pytest.mark.parametrize(
    ("start", "end", "want"),
    [(4.0, 6.5, 3.1), (0.1, 2.0, 2.3)],  # padded both sides; clamped at the file start
)
def test_expected_clip_follows_anki_miners_padded_window(start, end, want):
    assert hp.expected_clip_s(start, end, PADDING) == pytest.approx(want)


def test_isolation_needs_a_private_anki_miner_home_and_home(tmp_path):
    owner = tmp_path / "owner"
    private = tmp_path / "private"

    assert hp.isolation_problems({"HOME": str(private), "ANKI_MINER_HOME": str(private / "am")}, owner) == []
    assert hp.isolation_problems({"HOME": str(private)}, owner) == ["ANKI_MINER_HOME is not set"]
    assert hp.isolation_problems({"HOME": str(owner), "ANKI_MINER_HOME": str(private)}, owner) == [
        f"HOME is the real home {owner}"
    ]
    assert hp.isolation_problems({"HOME": str(private), "ANKI_MINER_HOME": str(owner / ".anki_miner")}, owner) == [
        f"ANKI_MINER_HOME is the real Anki Miner home {owner / '.anki_miner'}"
    ]


def test_main_refuses_to_run_without_isolation(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("ANKI_MINER_HOME", raising=False)

    rc = hp.main(["--game-dir", str(tmp_path), "--work", str(tmp_path / "work")])

    assert rc == hp.EXIT_REFUSED
    assert "ANKI_MINER_HOME is not set" in capsys.readouterr().err


# --- the checks -----------------------------------------------------------------------------------


def test_a_well_formed_game_folder_passes_every_check(tmp_path):
    game = make_game(tmp_path / "Probe")
    am = FakeAnkiMiner()

    checks = hp.run(game, tmp_path / "work", am, clips=3, duration=fake_duration)

    assert [c.name for c in checks] == list(hp.CHECKS)
    assert all(c.passed for c in checks), [c for c in checks if not c.passed]
    assert len(am.extracted) == 3
    assert len({word.sentence for _, word in am.extracted}) == 3  # three different cues


def test_fewer_than_three_sessions_fail(tmp_path):
    game = make_game(tmp_path / "Probe", {"Probe - 01": SESSIONS["Probe - 01"]})

    checks = by_name(hp.run(game, tmp_path / "work", FakeAnkiMiner(), clips=1, duration=fake_duration))

    assert not checks["sessions"].passed


def test_a_subtitle_that_does_not_fill_in_fails_the_same_stem_check(tmp_path):
    game = make_game(tmp_path / "Probe")
    am = FakeAnkiMiner()
    am.sibling_subtitle = lambda video: None  # type: ignore[method-assign]

    checks = by_name(hp.run(game, tmp_path / "work", am, duration=fake_duration))

    assert not checks["same_stem_autofill"].passed
    assert checks["batch_pairing"].passed


def test_batch_pairing_must_pair_every_session_with_its_own_subtitle(tmp_path):
    game = make_game(tmp_path / "Probe")
    am = FakeAnkiMiner()
    videos = sorted(game.glob("*.mkv"))
    am.batch_pairs = lambda folder: [(videos[0], videos[1].with_suffix(".srt"))]  # type: ignore[method-assign]

    checks = by_name(hp.run(game, tmp_path / "work", am, duration=fake_duration))

    assert not checks["batch_pairing"].passed


def test_parsed_entries_must_match_the_cues(tmp_path):
    game = make_game(tmp_path / "Probe")
    am = FakeAnkiMiner()
    am.raw_entries = lambda subtitle: [(0.0, 1.0, "wrong")]  # type: ignore[method-assign]

    checks = by_name(hp.run(game, tmp_path / "work", am, duration=fake_duration))

    assert not checks["subtitle_parse"].passed


def test_words_must_carry_their_lines_timing(tmp_path):
    game = make_game(tmp_path / "Probe")
    am = FakeAnkiMiner()
    am.words = lambda subtitle: [Word("x", "x", 0.0, 0.5)]  # type: ignore[method-assign]

    checks = by_name(hp.run(game, tmp_path / "work", am, duration=fake_duration))

    assert not checks["subtitle_words"].passed


def test_a_folder_without_words_fails_the_word_check(tmp_path):
    game = make_game(tmp_path / "Probe")
    am = FakeAnkiMiner()
    am.words = lambda subtitle: []  # type: ignore[method-assign]

    checks = by_name(hp.run(game, tmp_path / "work", am, duration=fake_duration))

    assert not checks["subtitle_words"].passed
    assert not checks["audio_clips"].passed


def test_a_clip_of_the_wrong_length_fails(tmp_path):
    game = make_game(tmp_path / "Probe")

    def short(path: Path) -> float:
        return VIDEO_S if path.suffix == ".mkv" else 0.5

    checks = by_name(hp.run(game, tmp_path / "work", FakeAnkiMiner(), duration=short))

    assert not checks["audio_clips"].passed


def test_a_cue_whose_padded_end_runs_past_the_video_is_not_clipped(tmp_path):
    # Session 03's only cue ends at 4.0 s; a 4.1 s video cannot hold its padded end (4.3 s).
    game = make_game(tmp_path / "Probe")
    am = FakeAnkiMiner()

    def duration(path: Path) -> float:
        if path.suffix == ".mkv":
            return 4.1 if path.stem == "Probe - 03" else VIDEO_S
        return fake_duration(path)

    checks = by_name(hp.run(game, tmp_path / "work", am, clips=3, duration=duration))

    assert checks["audio_clips"].passed
    assert all(video.stem != "Probe - 03" for video, _ in am.extracted)
