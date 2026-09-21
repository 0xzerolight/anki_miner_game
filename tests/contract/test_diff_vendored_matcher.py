"""``scripts/diff_vendored_matcher.py`` reports drift between the vendored extractor and an Anki Miner checkout."""

from pathlib import Path

from scripts import diff_vendored_matcher as dvm

VENDORED = Path(__file__).resolve().parent / "anki_miner_episode_matcher.py"


def _checkout(root: Path, source: str) -> Path:
    target = root / "anki_miner" / "utils" / "episode_matcher.py"
    target.parent.mkdir(parents=True)
    target.write_text(source, encoding="utf-8")
    return root


def _upstream_like() -> str:
    """The vendored definitions wrapped in unrelated upstream code, as the real file has."""
    vendored = VENDORED.read_text(encoding="utf-8")
    body = vendored[vendored.index("@dataclass") :]
    return (
        '"""Episode number extraction and matching for video/subtitle pairs."""\n\n'
        "import logging\nimport re\nfrom dataclasses import dataclass\nfrom pathlib import Path\n\n"
        "logger = logging.getLogger(__name__)\n\n\n" + body + "\n\nclass EpisodeMatcher:\n    pass\n"
    )


def test_identical_definitions_exit_zero(tmp_path, capsys):
    root = _checkout(tmp_path, _upstream_like())
    assert dvm.main([str(root)]) == 0
    assert "no drift" in capsys.readouterr().out


def test_a_changed_pattern_is_reported_with_a_diff(tmp_path, capsys):
    source = _upstream_like().replace('YEAR_LIKE = r"(?:19|20)\\d{2}"', 'YEAR_LIKE = r"(?:19|20|21)\\d{2}"')
    root = _checkout(tmp_path, source)
    assert dvm.main([str(root)]) == 1
    out = capsys.readouterr().out
    assert "EpisodeNumberExtractor" in out
    assert '+    YEAR_LIKE = r"(?:19|20|21)\\d{2}"' in out


def test_a_missing_definition_is_drift(tmp_path, capsys):
    source = _upstream_like().replace("def _strip_technical_tokens", "def _strip_release_tokens")
    root = _checkout(tmp_path, source)
    assert dvm.main([str(root)]) == 1
    assert "_strip_technical_tokens: missing" in capsys.readouterr().out


def test_no_checkout_is_a_usage_error(tmp_path, capsys):
    assert dvm.main([str(tmp_path / "nowhere")]) == 2
    assert dvm.main([]) == 2
    assert "usage" in capsys.readouterr().err


def test_the_vendored_copy_names_its_commit():
    assert dvm.vendored_commit(VENDORED) == "ea4a30ce2be4f57f30379ca3fe1ec438ff7fb8a5"
