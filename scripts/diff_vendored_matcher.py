"""Diff the vendored Anki Miner episode extractor against an Anki Miner checkout.

Usage: diff_vendored_matcher.py <anki_miner checkout>

Compares ``EpisodeInfo``, ``_strip_technical_tokens`` and ``EpisodeNumberExtractor`` in
``tests/contract/anki_miner_episode_matcher.py`` with the same definitions in the checkout's
``anki_miner/utils/episode_matcher.py``. Exit 0: no drift. Exit 1: drift, with a unified diff per
definition. Exit 2: usage error or no such file. A dev tool, not part of CI (CI has no checkout).
"""

import ast
import difflib
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VENDORED = REPO / "tests" / "contract" / "anki_miner_episode_matcher.py"
UPSTREAM = Path("anki_miner") / "utils" / "episode_matcher.py"
USAGE = "usage: diff_vendored_matcher.py <anki_miner checkout>"
DEFINITIONS = ("EpisodeInfo", "_strip_technical_tokens", "EpisodeNumberExtractor")


def definitions(path: Path) -> dict[str, list[str]]:
    """Top-level classes and functions in ``path``, as source lines including their decorators."""
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines(keepends=True)
    found: dict[str, list[str]] = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.ClassDef | ast.FunctionDef):
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            found[node.name] = lines[start - 1 : node.end_lineno]
    return found


def vendored_commit(path: Path) -> str | None:
    match = re.search(r'^VENDORED_COMMIT = "([0-9a-f]+)"$', path.read_text(encoding="utf-8"), re.MULTILINE)
    return match.group(1) if match else None


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(USAGE, file=sys.stderr)
        return 2
    upstream = Path(argv[0]) / UPSTREAM
    if not upstream.is_file():
        print(f"{USAGE}\nno {UPSTREAM} under {argv[0]}", file=sys.stderr)
        return 2

    ours, theirs = definitions(VENDORED), definitions(upstream)
    drift = False
    for name in DEFINITIONS:
        if name not in theirs:
            print(f"{name}: missing from {upstream}")
            drift = True
            continue
        diff = list(difflib.unified_diff(ours[name], theirs[name], f"vendored/{name}", f"{upstream}/{name}"))
        if diff:
            print(f"{name}: differs")
            sys.stdout.writelines(diff)
            drift = True

    print(f"vendored from Anki Miner {vendored_commit(VENDORED)}; compared with {upstream}")
    if drift:
        print("drift: re-vendor the definitions above and update VENDORED_COMMIT, then run the contract test")
        return 1
    print("no drift")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
