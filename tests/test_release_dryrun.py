"""``scripts/release_dryrun.sh`` (master plan T29): dispatches ``release.yml`` on the current branch and, after a
green run, proves the dry run built and smoked every selected leg and created no tag and no GitHub Release.

The script runs against a fake ``gh`` (canned run, jobs, logs and releases; its ``--jq`` goes through the
real jq, as gh's does) and a real git origin in a temporary directory, with ``sleep`` stubbed out."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "release_dryrun.sh"
BRANCH = "feat/x"

pytestmark = [
    pytest.mark.skipif(sys.platform == "win32", reason="release_dryrun.sh is a bash script"),
    pytest.mark.skipif(shutil.which("jq") is None, reason="jq is not installed"),
]

FAKE_GH = """#!PYTHON
import json, os, subprocess, sys
from pathlib import Path

state = Path(os.environ["FAKE_GH_STATE"])
scenario = json.loads((state / "scenario.json").read_text())
args = sys.argv[1:]
with open(state / "calls.log", "a") as calls:
    calls.write(json.dumps(args) + "\\n")


def opt(name):
    return args[args.index(name) + 1] if name in args else None


def out(data):
    raw = json.dumps(data)
    expr = opt("--jq")
    if expr is None:
        print(raw)
    else:
        sys.stdout.write(subprocess.run(["jq", "-r", expr], input=raw, capture_output=True, text=True, check=True).stdout)


dispatched = (state / "dispatched").exists()
watched = (state / "watched").exists()
if args[:2] == ["workflow", "run"]:
    (state / "dispatched").touch()
elif args[:2] == ["run", "list"]:
    out(([{"databaseId": 101}] if dispatched else []) + [{"databaseId": 100}])
elif args[:2] == ["run", "watch"]:
    (state / "watched").touch()
    if scenario.get("tag_during_run"):
        subprocess.run(
            ["git", "--git-dir", os.environ["FAKE_ORIGIN"], "tag", scenario["tag_during_run"], "refs/heads/BRANCH"],
            check=True,
        )
    sys.exit(scenario.get("watch_rc", 0))
elif args[:2] == ["run", "view"]:
    if "--log-failed" in args:
        print("failed step log")
    elif "--log" in args:
        sys.stdout.write(scenario["logs"].get(opt("--job"), ""))
    elif opt("--json") == "url":
        out({"url": "https://github.invalid/runs/101"})
    else:
        out({"jobs": scenario["jobs"], "conclusion": "success"})
elif args[:2] == ["release", "list"]:
    out([{"tagName": tag} for tag in scenario["releases_after" if watched else "releases"]])
else:
    sys.exit(f"fake gh: unexpected {args}")
""".replace("PYTHON", sys.executable).replace("BRANCH", BRANCH)


def jobs(*legs: str, **conclusions: str) -> list[dict[str, object]]:
    """The run's jobs: setup, the skipped tag-only jobs, and a successful build per leg; overrides by name."""
    listed = [("setup", "success"), ("ci-gate", "skipped")]
    listed += [(f"build {leg}", "success") for leg in legs]
    listed += [("release", "skipped")]
    found = []
    for number, (name, conclusion) in enumerate(listed, start=1):
        key = name.replace(" ", "_").replace("-", "_")
        if conclusions.get(key) == "absent":
            continue
        found.append({"name": name, "conclusion": conclusions.get(key, conclusion), "databaseId": number})
    return found


GREEN_LOGS = {"linux": "PASS self-check\nall green\n", "windows": "PASS self-check\nall green\nINSTALLER_SMOKE_PASS\n"}


def scenario(*legs: str, logs: dict[str, str] | None = None, **overrides: object) -> dict[str, object]:
    job_overrides = {k: str(v) for k, v in overrides.items() if k in ("ci_gate", "release") or k.startswith("build_")}
    listed = jobs(*legs, **job_overrides)
    by_leg = {**GREEN_LOGS, **(logs or {})}
    return {
        "jobs": listed,
        "logs": {str(j["databaseId"]): by_leg.get(str(j["name"]).removeprefix("build "), "") for j in listed},
        "releases": ["v0.0.9"],
        "releases_after": overrides.get("releases_after", ["v0.0.9"]),
        "watch_rc": overrides.get("watch_rc", 0),
        "tag_during_run": overrides.get("tag_during_run"),
    }


def git(*args: str, cwd: Path | None = None) -> str:
    environ = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True, env=environ).stdout


class Rig:
    def __init__(self, root: Path) -> None:
        self.origin = root / "origin.git"
        self.work = root / "work"
        self.state = root / "gh-state"
        self.bin = root / "bin"
        for directory in (self.state, self.bin):
            directory.mkdir()
        git("init", "--bare", "-q", "-b", "main", str(self.origin))
        git("clone", "-q", str(self.origin), str(self.work))
        git("checkout", "-q", "-b", BRANCH, cwd=self.work)
        (self.work / "scripts").mkdir()
        shutil.copy(SCRIPT, self.work / "scripts" / "release_dryrun.sh")
        git("add", "scripts", cwd=self.work)
        git("commit", "-q", "-m", "init", cwd=self.work)
        git("tag", "v0.0.9", cwd=self.work)
        git("push", "-q", "origin", BRANCH, "v0.0.9", cwd=self.work)
        for name, body in (("gh", FAKE_GH), ("sleep", "#!/bin/sh\nexit 0\n")):
            (self.bin / name).write_text(body, encoding="utf-8")
            (self.bin / name).chmod(0o755)

    def run(self, *args: str, scenario: dict[str, object]) -> subprocess.CompletedProcess[str]:
        (self.state / "scenario.json").write_text(json.dumps(scenario), encoding="utf-8")
        environ = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "FAKE_GH_STATE": str(self.state),
            "FAKE_ORIGIN": str(self.origin),
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        return subprocess.run(
            ["bash", str(self.work / "scripts" / "release_dryrun.sh"), *args],
            cwd=self.work,
            capture_output=True,
            text=True,
            timeout=120,
            env=environ,
            check=False,
        )

    def calls(self) -> list[list[str]]:
        log = self.state / "calls.log"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    def dispatches(self) -> list[list[str]]:
        return [call for call in self.calls() if call[:2] == ["workflow", "run"]]


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def test_a_green_dry_run_of_both_platforms(rig):
    result = rig.run(scenario=scenario("linux", "windows"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RELEASE DRY-RUN GREEN" in result.stdout
    assert rig.dispatches() == [["workflow", "run", "release.yml", "--ref", BRANCH, "-f", "platforms=all"]]


def test_a_single_platform_needs_only_its_own_leg(rig):
    result = rig.run("linux", scenario=scenario("linux"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RELEASE DRY-RUN GREEN" in result.stdout
    assert rig.dispatches() == [["workflow", "run", "release.yml", "--ref", BRANCH, "-f", "platforms=linux"]]


def test_an_unknown_platform_is_a_usage_error_before_any_dispatch(rig):
    result = rig.run("macos", scenario=scenario("linux", "windows"))
    assert result.returncode == 2
    assert rig.dispatches() == []


def test_a_branch_missing_from_origin_is_refused_before_any_dispatch(rig):
    git("checkout", "-q", "-b", "feat/local-only", cwd=rig.work)
    result = rig.run(scenario=scenario("linux", "windows"))
    assert result.returncode == 2
    assert "not on origin" in result.stderr
    assert rig.dispatches() == []


def test_an_unpushed_commit_is_refused_before_any_dispatch(rig):
    """The run builds origin's branch head; a green result must be about the commit checked out here."""
    git("commit", "-q", "--allow-empty", "-m", "later", cwd=rig.work)
    result = rig.run(scenario=scenario("linux", "windows"))
    assert result.returncode == 2
    assert "push it first" in result.stderr.lower()
    assert rig.dispatches() == []


def test_a_red_run_fails_with_its_failed_log(rig):
    result = rig.run(scenario=scenario("linux", "windows", watch_rc=1))
    assert result.returncode == 1
    assert "RELEASE DRY-RUN FAILED" in result.stdout
    assert "failed step log" in result.stdout
    assert "GREEN" not in result.stdout


@pytest.mark.parametrize(
    ("overrides", "why"),
    [
        ({"release": "success"}, "the release job ran"),
        ({"ci_gate": "success"}, "the ci-gate job ran"),
        ({"release": "absent"}, "the release job is gone, so its skip proves nothing"),
        ({"ci_gate": "absent"}, "the ci-gate job is gone"),
        ({"build_windows": "skipped"}, "a leg was skipped: the run is green without building it"),
        ({"build_linux": "absent"}, "a selected leg never ran"),
        ({"releases_after": ["v0.0.9", "v0.1.0"]}, "a GitHub Release appeared"),
        ({"tag_during_run": "v0.1.0"}, "a tag appeared on origin"),
    ],
)
def test_a_green_run_that_breaks_a_dry_run_guarantee_fails(rig, overrides, why):
    result = rig.run(scenario=scenario("linux", "windows", **overrides))
    assert result.returncode == 1, why
    assert "RELEASE DRY-RUN FAILED" in result.stdout, why
    assert "GREEN" not in result.stdout, why


@pytest.mark.parametrize(
    "logs",
    [
        {"linux": "FAIL self-check\n"},
        {"windows": "PASS self-check\n"},  # the installer smoke never printed its marker
        {"windows": "INSTALLER_SMOKE_PASS\n"},  # the bundle smoke never ran on Windows
    ],
    ids=["linux-bundle-smoke", "windows-installer-smoke", "windows-bundle-smoke"],
)
def test_a_green_run_whose_smoke_left_no_marker_fails(rig, logs):
    result = rig.run(scenario=scenario("linux", "windows", logs=logs))
    assert result.returncode == 1
    assert "RELEASE DRY-RUN FAILED" in result.stdout
    assert "GREEN" not in result.stdout


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck is not installed")
def test_the_script_passes_shellcheck():
    result = subprocess.run(["shellcheck", str(SCRIPT)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
