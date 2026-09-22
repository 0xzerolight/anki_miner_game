"""The hosted workflows (spec 19, master plan D3 and T29): ``ci.yml`` runs lint, typecheck and the tests on
Python 3.12 and 3.13 on Linux and Windows, for pushes and pull requests to ``main``; ``release.yml`` builds,
smokes and packages the Linux and Windows bundles on a ``v*`` tag and publishes them, and a
``workflow_dispatch`` of it is the dry run, which never tags or releases (``scripts/release_dryrun.sh``
proves that).

The venv has no YAML parser, so the helpers below read the two files by their indentation, which these
files keep regular. The shell steps whose logic matters (the version check and the matrix filter) are run
for real against temporary checkouts; the rest is pinned against the files the steps build from."""

import fnmatch
import json
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import anki_miner_game
from anki_miner_game.runtime.bundle_smoke import FAIL_MARKER, PASS_MARKER, SMOKE_ENV

REPO = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO / ".github" / "workflows"
CI = WORKFLOWS / "ci.yml"
RELEASE = WORKFLOWS / "release.yml"
AUTOMERGE = WORKFLOWS / "dependabot-automerge.yml"
MATRIX = REPO / ".github" / "release-matrix.json"
DRYRUN = REPO / "scripts" / "release_dryrun.sh"
INSTALLER_SMOKE = REPO / "scripts" / "windows_installer_smoke.ps1"
ISS = REPO / "packaging" / "innosetup" / "anki_miner_game.iss"
APPIMAGE_SCRIPT = REPO / "packaging" / "appimage" / "build-appimage.sh"
TARBALL_SCRIPT = REPO / "packaging" / "build-tarball.sh"

VERSION_EXPR = "${{ needs.setup.outputs.version }}"
BUNDLE_SMOKE_MARKER = "PASS self-check"  # scripts/bundle_smoke.sh's line for the in-app self-check
INSTALLER_SMOKE_MARKER = "INSTALLER_SMOKE_PASS"

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="runs the workflow's bash steps")
needs_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is not installed")


# --- reading the workflows ----------------------------------------------------------------------


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def job(workflow: Path, job_id: str) -> str:
    """One job's lines, from under its ``  <id>:`` key to the next job."""
    jobs = text(workflow).split("\njobs:\n", 1)[1]
    match = re.search(rf"^  {re.escape(job_id)}:\n(.*?)(?=^  \S|\Z)", jobs, re.M | re.S)
    assert match, f"no job {job_id} in {workflow.name}"
    return match.group(1)


def job_ids(workflow: Path) -> list[str]:
    return re.findall(r"^  ([\w-]+):$", text(workflow).split("\njobs:\n", 1)[1], re.M)


def job_key(job_text: str, key: str) -> str | None:
    match = re.search(rf"^    {key}: (.+)$", job_text, re.M)
    return match.group(1).strip() if match else None


def steps(job_text: str) -> list[str]:
    body = job_text.split("\n    steps:\n", 1)[1]
    return ["      - " + chunk for chunk in re.split(r"^      - ", body, flags=re.M)[1:]]


def step(job_text: str, name: str) -> str:
    found = [s for s in steps(job_text) if field(s, "name") == name]
    assert len(found) == 1, f"{len(found)} steps named {name!r}"
    return found[0]


def field(step_text: str, key: str) -> str | None:
    match = re.search(rf"^(?:      - |        ){key}: (.+)$", step_text, re.M)
    return match.group(1).strip() if match else None


def block(step_text: str, key: str, indent: int = 8) -> str:
    """A ``key: |`` block scalar at ``indent`` spaces, dedented; or the key's inline value."""
    lines = step_text.splitlines()
    head = re.compile(rf"^(?:{' ' * (indent - 2)}- |{' ' * indent}){key}: ?(.*)$")
    for i, line in enumerate(lines):
        match = head.match(line)
        if not match:
            continue
        if match.group(1) != "|":
            return match.group(1)
        body = []
        for following in lines[i + 1 :]:
            if following.strip() and len(following) - len(following.lstrip()) <= indent:
                break
            body.append(following)
        return textwrap.dedent("\n".join(body)).strip("\n") + "\n"
    raise AssertionError(f"no {key} in step:\n{step_text}")


def env(step_text: str) -> dict[str, str]:
    match = re.search(r"^        env:\n((?:          .+\n)+)", step_text, re.M)
    if not match:
        return {}
    pairs = (line.strip().split(": ", 1) for line in match.group(1).splitlines())
    return {key: value.strip('"') for key, value in pairs}


def uses(workflow: Path) -> list[str]:
    return re.findall(r"^\s+(?:- )?uses: (\S+.*)$", text(workflow), re.M)


def run_bash(script: str, cwd: Path, **environ: str) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    assert bash is not None
    return subprocess.run(
        [bash, "-c", script],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": "/usr/bin:/bin", **environ},
        check=False,
    )


def outputs(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines() if "=" in line)


def matrix() -> list[dict[str, str]]:
    return json.loads(text(MATRIX))


# --- ci.yml (D3) --------------------------------------------------------------------------------


def test_ci_runs_on_pushes_and_pull_requests_to_main_and_on_demand():
    on = text(CI).split("\non:\n", 1)[1].split("\n\n", 1)[0]
    assert re.search(r"^  push:\n    branches: \[main\]$", on, re.M)
    assert re.search(r"^  pull_request:\n    branches: \[main\]$", on, re.M)
    assert re.search(r"^  workflow_dispatch:$", on, re.M)
    assert "tags" not in on


def test_ci_lints_typechecks_and_tests_both_pythons_on_linux_and_windows():
    assert {"lint", "typecheck", "test"} <= set(job_ids(CI))
    test = job(CI, "test")
    assert re.search(r"^        os: \[ubuntu-latest, windows-latest\]$", test, re.M)
    assert re.search(r'^        python-version: \["3\.12", "3\.13"\]$', test, re.M)
    assert re.search(r"^      fail-fast: false$", test, re.M)


def test_ci_installs_the_validators_the_packaging_tests_otherwise_skip():
    """tests/test_installers.py skips shellcheck, desktop-file-validate and appstreamcli when they are absent."""
    apt = block(step(job(CI, "test"), "Install system libraries"), "run")
    packages = set(re.search(r"apt-get install -y (.+)", apt).group(1).split())
    assert {"shellcheck", "desktop-file-utils", "appstream"} <= packages
    assert {"libegl1", "libxkbcommon0"} <= packages  # Qt offscreen


# --- both workflows -----------------------------------------------------------------------------


@pytest.mark.parametrize("workflow", [CI, RELEASE, AUTOMERGE], ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_commit(workflow):
    found = uses(workflow)
    assert found
    for ref in found:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40} # v[\d.]+", ref), ref


@pytest.mark.parametrize("workflow", [CI, RELEASE], ids=lambda p: p.name)
def test_the_default_token_is_read_only(workflow):
    assert re.search(r"^permissions:\n  contents: read$", text(workflow), re.M)


# --- release.yml: triggers and the dry-run guards -----------------------------------------------


def test_release_runs_on_a_version_tag_or_a_dispatch_only():
    on = text(RELEASE).split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
    assert re.search(r'^  push:\n    tags:\n      - "v\*"$', on, re.M)
    assert "branches" not in on and "pull_request" not in on and "schedule" not in on
    inputs = re.findall(r"^      (\w+):$", on.split("workflow_dispatch:", 1)[1], re.M)
    assert inputs == ["platforms"], "an undeclared -f key is HTTP 422; the dry-run script passes platforms only"


def test_the_dispatch_choices_are_all_and_each_platform_of_the_matrix():
    on = text(RELEASE).split("workflow_dispatch:", 1)[1].split("\npermissions:", 1)[0]
    options = re.findall(r"^          - (\S+)$", on, re.M)
    assert options == ["all", *[entry["platform"] for entry in matrix()]]
    assert re.search(r"^        default: all$", on, re.M)


def test_the_matrix_is_linux_and_windows_only():
    """macOS is unsupported (global constraints)."""
    entries = matrix()
    assert [entry["platform"] for entry in entries] == ["linux", "windows"]
    assert {entry["os"] for entry in entries} == {"ubuntu-22.04", "windows-latest"}
    assert all(set(entry) == {"platform", "os", "artifact_name"} for entry in entries)
    assert len({entry["artifact_name"] for entry in entries}) == 2
    assert "macos" not in text(RELEASE).lower().replace("macos is unsupported", "")


@pytest.mark.parametrize("job_id", ["ci-gate", "release"])
def test_the_tag_only_jobs_are_guarded_by_the_event_alone(job_id):
    """A dispatch skips both, which is what makes it a dry run; release_dryrun.sh asserts the skip."""
    assert job_key(job(RELEASE, job_id), "if") == "github.event_name == 'push'"


def test_the_release_job_waits_for_every_build_leg():
    """Its guard is the event alone (no always() or !cancelled()), so a red leg keeps it from publishing."""
    release = job(RELEASE, "release")
    assert "build" in re.findall(r"[\w-]+", job_key(release, "needs"))
    assert job_key(release, "if") == "github.event_name == 'push'"


def test_the_build_runs_on_a_green_ci_gate_or_a_dispatch():
    build = job(RELEASE, "build")
    guard = job_key(build, "if")
    assert "needs.setup.result == 'success'" in guard
    assert "needs.ci-gate.result == 'success' || needs.ci-gate.result == 'skipped'" in guard
    assert job_key(build, "name") == "build ${{ matrix.platform }}"  # release_dryrun.sh finds legs by it
    assert job_key(build, "runs-on") == "${{ matrix.os }}"


def test_the_ci_gate_waits_for_a_green_ci_run_on_the_tagged_commit():
    gate = job(RELEASE, "ci-gate")
    script = block(step(gate, "Require green CI on the tagged commit"), "run")
    assert "actions/workflows/ci.yml/runs?head_sha=${GITHUB_SHA}" in script
    assert "--paginate" in script
    assert re.search(r"^      actions: read$", gate, re.M)


def test_only_the_release_job_may_write():
    for job_id in job_ids(RELEASE):
        writes = "contents: write" in job(RELEASE, job_id)
        assert writes == (job_id == "release"), job_id


# --- release.yml: the setup job's steps, run for real -------------------------------------------


def setup_step(name: str) -> tuple[str, dict[str, str]]:
    found = step(job(RELEASE, "setup"), name)
    return block(found, "run"), env(found)


def version_checkout(root: Path, version: str, notes: bool = True) -> Path:
    package = root / "anki_miner_game"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(f'"""Doc."""\n\n__version__ = "{version}"\n', encoding="utf-8")
    if notes:
        (root / "docs" / "release_notes").mkdir(parents=True)
        (root / "docs" / "release_notes" / f"v{version}.md").write_text("### Added\n\n- **X.**\n", encoding="utf-8")
    return root


def check_version(root: Path, event: str, ref: str) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    script, step_env = setup_step("Validate the version")
    assert step_env == {"EVENT_NAME": "${{ github.event_name }}", "REF_NAME": "${{ github.ref_name }}"}
    out = root / "github_output"
    out.touch()
    result = run_bash(script, root, EVENT_NAME=event, REF_NAME=ref, GITHUB_OUTPUT=str(out))
    return result, outputs(out)


@posix_only
def test_a_tag_matching_the_version_with_its_release_notes_passes(tmp_path):
    result, out = check_version(version_checkout(tmp_path, "1.2.3"), "push", "v1.2.3")
    assert result.returncode == 0, result.stdout + result.stderr
    assert out == {"version": "1.2.3"}


@posix_only
@pytest.mark.parametrize(
    ("version", "ref", "notes"),
    [
        ("1.2.3", "v1.2.4", True),  # the tag is not __version__
        ("1.2.3", "1.2.3", True),  # a tag without its v
        ("1.2.3", "v1.2.3", False),  # no docs/release_notes/v1.2.3.md for the release body
        ("1.2.3rc1", "v1.2.3rc1", True),  # the Windows installer needs a plain X.Y.Z
    ],
)
def test_a_tag_push_fails_on_any_mismatch(tmp_path, version, ref, notes):
    result, out = check_version(version_checkout(tmp_path, version, notes), "push", ref)
    assert result.returncode != 0
    assert "::error::" in result.stdout
    assert "version" not in out


@posix_only
def test_a_dispatch_takes_the_version_from_the_code_without_notes(tmp_path):
    result, out = check_version(version_checkout(tmp_path, "1.2.3", notes=False), "workflow_dispatch", "feat/x")
    assert result.returncode == 0, result.stdout + result.stderr
    assert out == {"version": "1.2.3"}


@posix_only
def test_a_dispatch_still_refuses_a_version_the_installer_cannot_carry(tmp_path):
    result, _out = check_version(version_checkout(tmp_path, "1.2", notes=False), "workflow_dispatch", "main")
    assert result.returncode != 0


@posix_only
def test_the_version_check_reads_the_real_version_file(tmp_path):
    shutil.copytree(REPO / "anki_miner_game", tmp_path / "anki_miner_game", ignore=shutil.ignore_patterns("*.pyc"))
    result, out = check_version(tmp_path, "workflow_dispatch", "main")
    assert result.returncode == 0, result.stdout + result.stderr
    assert out == {"version": anki_miner_game.__version__}


def select_platforms(tmp_path: Path, platforms: str) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    script, step_env = setup_step("Build the matrix")
    assert step_env == {"PLATFORMS": "${{ github.event_name == 'push' && 'all' || inputs.platforms }}"}
    (tmp_path / ".github").mkdir(exist_ok=True)
    shutil.copy(MATRIX, tmp_path / ".github" / "release-matrix.json")
    out = tmp_path / f"github_output_{platforms}"
    out.touch()
    result = run_bash(script, tmp_path, PLATFORMS=platforms, GITHUB_OUTPUT=str(out))
    return result, outputs(out)


@posix_only
@needs_jq
@pytest.mark.parametrize(
    ("platforms", "legs"), [("all", ["linux", "windows"]), ("linux", ["linux"]), ("windows", ["windows"])]
)
def test_the_matrix_step_selects_the_dispatched_platforms(tmp_path, platforms, legs):
    result, out = select_platforms(tmp_path, platforms)
    assert result.returncode == 0, result.stdout + result.stderr
    include = json.loads(out["matrix"])["include"]
    assert [entry["platform"] for entry in include] == legs
    assert include == [entry for entry in matrix() if entry["platform"] in legs]


@posix_only
@needs_jq
@pytest.mark.parametrize("platforms", ["macos", ""])
def test_the_matrix_step_fails_on_a_selection_with_no_platform(tmp_path, platforms):
    result, out = select_platforms(tmp_path, platforms)
    assert result.returncode != 0
    assert "matrix" not in out


# --- release.yml: the build legs ----------------------------------------------------------------


def build_step(name: str) -> str:
    return step(job(RELEASE, "build"), name)


def test_the_build_uses_the_locked_build_env_on_python_312():
    build = job(RELEASE, "build")
    assert re.search(r'^          python-version: "3\.12"$', build, re.M)
    assert re.search(r"^          enable-cache: false$", build, re.M)  # a dispatch builds as a tag push does
    linux = block(build_step("Install the build environment (Linux)"), "run")
    assert linux.split() == shlex.split("uv sync --locked --no-install-project --extra build")
    assert "--extra dev" not in build  # the frozen app carries the runtime dependencies only
    assert block(build_step("Build the bundle"), "run").strip() == (
        "uv run --no-sync pyinstaller --noconfirm anki_miner_game.spec"
    )


def test_the_windows_bootloader_is_compiled_from_source_and_proved():
    install = build_step("Install the build environment (Windows, bootloader from source)")
    assert field(install, "if") == "runner.os == 'Windows'"
    assert env(install) == {"PYINSTALLER_COMPILE_BOOTLOADER": "1"}
    assert "--no-binary-package pyinstaller" in block(install, "run")
    proof = build_step("Prove the bootloader is not the prebuilt one (Windows)")
    assert field(proof, "if") == "runner.os == 'Windows'"
    script = block(proof, "run")
    assert "uv.lock" in script and "win_amd64.whl" in script and "runw.exe" in script


def test_every_leg_smokes_its_bundle():
    smoke = build_step("Bundle smoke")
    assert field(smoke, "if") is None
    assert field(smoke, "shell") == "bash"
    assert block(smoke, "run").strip() == "bash scripts/bundle_smoke.sh dist/AnkiMinerGame"


def test_the_smoke_markers_the_dry_run_looks_for_are_printed_only_by_the_smokes():
    """GitHub echoes each step's script into the job log, so a marker written in release.yml would prove nothing."""
    assert BUNDLE_SMOKE_MARKER in text(DRYRUN) and INSTALLER_SMOKE_MARKER in text(DRYRUN)
    assert BUNDLE_SMOKE_MARKER not in text(RELEASE) and INSTALLER_SMOKE_MARKER not in text(RELEASE)
    assert 'check "self-check"' in text(REPO / "scripts" / "bundle_smoke.sh")
    assert f"Write-Host '{INSTALLER_SMOKE_MARKER}'" in text(INSTALLER_SMOKE)


def rendered(value: str) -> str:
    return value.strip().strip('"').replace(VERSION_EXPR, "1.2.3")


def uploads(os_label: str) -> list[str]:
    upload = build_step(f"Upload the artifacts ({os_label})")
    assert field(upload, "if") == f"runner.os == '{os_label}'"
    assert re.search(r"^          if-no-files-found: error$", upload, re.M)
    assert re.search(r"^          name: \$\{\{ matrix\.artifact_name \}\}$", upload, re.M)
    return [rendered(line) for line in block(upload, "path", indent=10).splitlines()]


def script_output(script: Path) -> str:
    """The ``OUT=`` path a packaging script writes, for version 1.2.3, relative to the repo."""
    out = re.search(r'^OUT="\$REPO_ROOT/(.+)"$', text(script), re.M).group(1)
    return out.replace("${VERSION}", "1.2.3")


def iss_output() -> str:
    setup = dict(re.findall(r"^(\w+)=(.+)$", text(ISS).split("[Setup]", 1)[1].split("\n[", 1)[0], re.M))
    assert setup["OutputDir"] == "..\\..\\dist"
    return "dist/" + setup["OutputBaseFilename"].replace("{#AppVersion}", "1.2.3") + ".exe"


def test_the_linux_leg_uploads_the_appimage_the_tarball_and_the_deb_the_scripts_build():
    deb = block(build_step("Build the .deb (Linux)"), "run")
    target = rendered(re.search(r'--target ("[^"]+"|\S+)', deb).group(1).replace("${VERSION}", VERSION_EXPR))
    assert target == "dist/anki-miner-game_1.2.3_amd64.deb"
    assert "--config packaging/nfpm.yaml --packager deb" in deb
    assert sorted(uploads("Linux")) == sorted([script_output(APPIMAGE_SCRIPT), script_output(TARBALL_SCRIPT), target])


def test_the_windows_leg_builds_smokes_and_uploads_the_installer_the_iss_names():
    build = block(build_step("Build the Windows installer"), "run")
    assert f"iscc /DAppVersion={VERSION_EXPR} packaging\\innosetup\\anki_miner_game.iss" in build
    assert uploads("Windows") == [iss_output()]
    smoke = build_step("Windows installer smoke")
    assert field(smoke, "if") == "runner.os == 'Windows'"
    args = shlex.split(block(smoke, "run"))
    assert args[0] == "./scripts/windows_installer_smoke.ps1"
    assert rendered(args[args.index("-Installer") + 1]) == iss_output()
    assert args[args.index("-Version") + 1] == VERSION_EXPR


def test_the_linux_packaging_steps_run_in_build_order():
    names = [field(s, "name") for s in steps(job(RELEASE, "build"))]
    order = ["Build the bundle", "Bundle smoke", "Build the AppImage (Linux)", "Build the .deb (Linux)"]
    assert [names.index(name) for name in order] == sorted(names.index(name) for name in order)
    # nfpm reads dist/anki-miner-game.metainfo.xml, which the AppImage build renders.
    assert "dist/anki-miner-game.metainfo.xml" in text(REPO / "packaging" / "nfpm.yaml")


def test_downloaded_build_tools_are_pinned_and_verified():
    inno = block(build_step("Install Inno Setup (Windows)"), "run")
    assert re.search(r"issrc/releases/download/is-7_0_2/innosetup-7\.0\.2-x64\.exe", inno)
    assert re.search(r"\$expectedSha = '[0-9a-f]{64}'", inno)
    nfpm = block(build_step("Install nfpm (Linux)"), "run")
    assert re.search(r'NFPM_SHA256="[0-9a-f]{64}"', nfpm)
    assert "sha256sum -c" in nfpm


def test_the_release_publishes_every_uploaded_file_with_the_committed_notes():
    publish = step(job(RELEASE, "release"), "Create the GitHub Release")
    globs = [line.strip() for line in block(publish, "files", indent=10).splitlines()]
    uploaded = [Path(path).name for path in uploads("Linux") + uploads("Windows")]
    for name in uploaded:
        assert any(fnmatch.fnmatch(f"artifacts/x/{name}", pattern.replace("**/", "")) for pattern in globs), name
    for pattern in globs:
        assert any(fnmatch.fnmatch(f"artifacts/x/{name}", pattern.replace("**/", "")) for name in uploaded), pattern
    assert rendered(block(publish, "body_path", indent=10)) == "docs/release_notes/v1.2.3.md"
    assert re.search(r"^          fail_on_unmatched_files: true$", publish, re.M)


# --- the Windows installer smoke script ---------------------------------------------------------


def test_the_installer_smoke_checks_the_install_the_app_and_the_uninstall():
    script = text(INSTALLER_SMOKE)
    app_id = re.search(r"^AppId=\{(\{[0-9A-F-]+\})$", text(ISS), re.M).group(1)
    assert f"'{app_id}'" in script  # the per-user uninstall key is <AppId>_is1
    assert re.search(r"^DefaultDirName=\{autopf\}\\AnkiMinerGame$", text(ISS), re.M)
    assert "'Programs\\AnkiMinerGame'" in script  # {autopf} of a per-user install
    assert "'AnkiMinerGame.exe'" in script
    for flag in ("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"):
        assert flag in script
    for name in (SMOKE_ENV, "ANKI_MINER_GAME_HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "QT_QPA_PLATFORM"):
        assert f"'{name}'" in script, name
    assert PASS_MARKER in script and FAIL_MARKER in script
    assert "config.json" in script and "anki_miner_game.log" in script and "DisplayVersion" in script
