"""The installers under packaging/ (spec 19): the Windows Inno Setup script, the Linux AppImage, `.deb`
(nfpm) and `.tar.gz` builds, and the launcher shim the Linux artifacts start the bundle through.

The real tools (ISCC, appimagetool, nfpm) run only in the release workflow; these tests pin what
each build reads and writes, run the shell parts against a fake bundle, and run the validators
(shellcheck, desktop-file-validate, appstreamcli) when the host has them."""

import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path

import pytest

from anki_miner_game.gui.settings_dialog import LINUX_CONTROL_NOTE

REPO = Path(__file__).resolve().parent.parent
PACKAGING = REPO / "packaging"
APP_NAME = "AnkiMinerGame"  # the bundle folder and executable anki_miner_game.spec builds
PACKAGE = "anki-miner-game"
# The command users bind for global control on Linux (spec 16): the pip entry point's name.
COMMAND = next(iter(tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"]))
APP_ID = "io.github._0xzerolight.AnkiMinerGame"
VERSION = "1.2.3"

LAUNCHER = PACKAGING / "linux-launcher.sh"
TARBALL_SCRIPT = PACKAGING / "build-tarball.sh"
APPIMAGE_SCRIPT = PACKAGING / "appimage" / "build-appimage.sh"
RENDER_METAINFO = PACKAGING / "render_metainfo.sh"
METAINFO_TEMPLATE = PACKAGING / "appstream" / f"{PACKAGE}.metainfo.xml.in"
NFPM = PACKAGING / "nfpm.yaml"
ISS = PACKAGING / "innosetup" / "anki_miner_game.iss"
DESKTOP_FILES = [PACKAGING / "appimage" / f"{PACKAGE}.desktop", PACKAGING / "deb" / f"{PACKAGE}.desktop"]
ICON_SIZES = (48, 64, 128, 256)

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="the Linux packaging scripts are bash")


def bash() -> str:
    found = shutil.which("bash")
    assert found is not None
    return found


def desktop_entry(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "[Desktop Entry]"
    return dict(line.split("=", 1) for line in lines[1:] if "=" in line)


# --- the launcher shim --------------------------------------------------------------------------

FAKE_APP = """#!/usr/bin/env bash
printf 'LD_PRELOAD=%s\\n' "${LD_PRELOAD:-}"
for arg in "$@"; do printf 'ARG=%s\\n' "$arg"; done
exit 7
"""


def fake_bundle(root: Path, libstdcxx: bytes | None = None) -> Path:
    bundle = root / APP_NAME
    (bundle / "_internal").mkdir(parents=True)
    app = bundle / APP_NAME
    app.write_text(FAKE_APP, encoding="utf-8")
    app.chmod(0o755)
    if libstdcxx is not None:
        (bundle / "_internal" / "libstdc++.so.6").write_bytes(libstdcxx)
    return bundle


def run_launcher(launcher: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    environ = {k: v for k, v in os.environ.items() if k not in ("LD_PRELOAD", "ANKI_MINER_GAME_NO_CXX_SHIM")}
    return subprocess.run(
        [bash(), str(launcher), *args], capture_output=True, text=True, timeout=30, env={**environ, **env}, check=False
    )


def host_libstdcxx() -> str | None:
    for ldconfig in (shutil.which("ldconfig"), "/sbin/ldconfig", "/usr/sbin/ldconfig"):
        if ldconfig and os.access(ldconfig, os.X_OK):
            out = subprocess.run([ldconfig, "-p"], capture_output=True, text=True, check=False).stdout
            for line in out.splitlines():
                if "libstdc++.so.6 (libc6,x86-64" in line:
                    return line.split()[-1]
    return None


@posix_only
def test_the_launcher_runs_the_bundle_with_its_arguments_and_exit_code(tmp_path):
    bundle = fake_bundle(tmp_path)
    result = run_launcher(LAUNCHER, "start", "two words", ANKI_MINER_GAME_BUNDLE_DIR=str(bundle))
    assert result.returncode == 7, result.stderr
    assert result.stdout.splitlines() == ["LD_PRELOAD=", "ARG=start", "ARG=two words"]


@posix_only
def test_the_launcher_finds_the_bundle_beside_itself_through_a_symlink(tmp_path):
    """The .deb puts /usr/bin/anki_miner_game -> /opt/anki-miner-game/anki-miner-game-launcher."""
    bundle = fake_bundle(tmp_path)
    launcher = bundle / "anki-miner-game-launcher"
    shutil.copy(LAUNCHER, launcher)
    (tmp_path / "bin").mkdir()
    link = tmp_path / "bin" / COMMAND
    link.symlink_to(launcher)
    result = run_launcher(link, "stop")
    assert result.returncode == 7, result.stderr
    assert "ARG=stop" in result.stdout.splitlines()


@posix_only
@pytest.mark.skipif(host_libstdcxx() is None, reason="no 64-bit libstdc++ known to ldconfig")
def test_the_launcher_preloads_a_newer_host_cxx_runtime(tmp_path):
    bundle = fake_bundle(tmp_path, libstdcxx=b"\0GLIBCXX_3.4\0GLIBCXX_3.4.1\0")
    result = run_launcher(LAUNCHER, ANKI_MINER_GAME_BUNDLE_DIR=str(bundle))
    preload = result.stdout.splitlines()[0].removeprefix("LD_PRELOAD=").split(":")
    assert preload[0] == host_libstdcxx()


@posix_only
@pytest.mark.skipif(host_libstdcxx() is None, reason="no 64-bit libstdc++ known to ldconfig")
def test_the_launcher_keeps_a_bundled_cxx_runtime_newer_than_the_host(tmp_path):
    bundle = fake_bundle(tmp_path, libstdcxx=b"\0GLIBCXX_3.4.999\0")
    result = run_launcher(LAUNCHER, ANKI_MINER_GAME_BUNDLE_DIR=str(bundle))
    assert result.stdout.splitlines()[0] == "LD_PRELOAD="


@posix_only
def test_the_launcher_shim_can_be_switched_off(tmp_path):
    bundle = fake_bundle(tmp_path, libstdcxx=b"\0GLIBCXX_3.4.1\0")
    result = run_launcher(LAUNCHER, ANKI_MINER_GAME_BUNDLE_DIR=str(bundle), ANKI_MINER_GAME_NO_CXX_SHIM="1")
    assert result.stdout.splitlines()[0] == "LD_PRELOAD="


# --- the .tar.gz --------------------------------------------------------------------------------


def fake_repo(tmp_path: Path) -> Path:
    """A repo root holding the packaging scripts and a built fake bundle in dist/."""
    repo = tmp_path / "repo"
    shutil.copytree(PACKAGING, repo / "packaging")
    shutil.copy(REPO / "LICENSE", repo / "LICENSE")
    fake_bundle(repo / "dist")
    return repo


@posix_only
def test_the_tarball_holds_the_bundle_the_launcher_and_the_licence_under_one_folder(tmp_path):
    repo = fake_repo(tmp_path)
    result = subprocess.run(
        [bash(), str(repo / "packaging" / "build-tarball.sh"), VERSION], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    archive = repo / "dist" / f"{APP_NAME}-{VERSION}-Linux-x86_64.tar.gz"
    with tarfile.open(archive) as tar:
        members = {m.name: m for m in tar.getmembers()}
    assert {name.split("/")[0] for name in members} == {APP_NAME}
    app = members[f"{APP_NAME}/{APP_NAME}"]
    assert app.isfile() and app.mode & 0o111
    launcher = members[f"{APP_NAME}/{COMMAND}"]
    assert launcher.isfile() and launcher.mode & 0o111
    assert f"{APP_NAME}/LICENSE" in members
    assert all(m.uid == 0 and m.gid == 0 for m in members.values())


@posix_only
def test_the_tarball_build_fails_without_a_bundle(tmp_path):
    repo = fake_repo(tmp_path)
    shutil.rmtree(repo / "dist" / APP_NAME)
    result = subprocess.run(
        [bash(), str(repo / "packaging" / "build-tarball.sh"), VERSION], capture_output=True, text=True, check=False
    )
    assert result.returncode != 0
    assert not (repo / "dist" / f"{APP_NAME}-{VERSION}-Linux-x86_64.tar.gz").exists()


# --- the AppImage -------------------------------------------------------------------------------


@posix_only
@pytest.mark.skipif(shutil.which("appstreamcli") is None, reason="appstreamcli is not installed")
def test_the_appimage_build_stages_an_appdir_that_starts_through_the_launcher(tmp_path):
    repo = fake_repo(tmp_path)
    result = subprocess.run(
        [bash(), str(repo / "packaging" / "appimage" / "build-appimage.sh"), VERSION],
        capture_output=True,
        text=True,
        env={**os.environ, "APPDIR_ONLY": "1"},
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    appdir = repo / "dist" / f"{APP_NAME}.AppDir"
    assert os.readlink(appdir / "AppRun") == f"usr/bin/{PACKAGE}-launcher"
    assert (appdir / "AppRun").is_file()
    assert (appdir / "usr" / "bin" / APP_NAME).is_file()
    assert desktop_entry(appdir / f"{PACKAGE}.desktop")["Icon"] == PACKAGE
    assert (appdir / f"{PACKAGE}.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert os.readlink(appdir / ".DirIcon") == f"{PACKAGE}.png"
    metainfo = appdir / "usr" / "share" / "metainfo" / f"{APP_ID}.metainfo.xml"
    assert f'<release version="{VERSION}"' in metainfo.read_text(encoding="utf-8")
    for px in ICON_SIZES:
        assert (appdir / "usr" / "share" / "icons" / "hicolor" / f"{px}x{px}" / "apps" / f"{PACKAGE}.png").is_file()
    assert not list((repo / "dist").glob("*.AppImage"))  # APPDIR_ONLY stops before appimagetool


def test_the_appimage_tool_is_pinned_to_a_version_and_a_sha256():
    text = APPIMAGE_SCRIPT.read_text(encoding="utf-8")
    assert re.search(r'^APPIMAGETOOL_VERSION="\d+\.\d+\.\d+"$', text, re.M)
    assert re.search(r'^APPIMAGETOOL_SHA256="[0-9a-f]{64}"$', text, re.M)
    assert "sha256sum -c" in text


# --- desktop entries, AppStream metadata, icons -------------------------------------------------


def test_the_desktop_entries_name_the_installed_icon_and_launcher():
    appimage, deb = (desktop_entry(path) for path in DESKTOP_FILES)
    assert appimage["Icon"] == deb["Icon"] == PACKAGE
    assert deb["Exec"] == f"/usr/bin/{COMMAND}"
    assert appimage["Name"] == deb["Name"] == "Anki Miner Game"


@pytest.mark.skipif(shutil.which("desktop-file-validate") is None, reason="desktop-file-validate is not installed")
@pytest.mark.parametrize("path", DESKTOP_FILES, ids=lambda p: p.parent.name)
def test_the_desktop_entries_validate(path):
    result = subprocess.run(["desktop-file-validate", str(path)], capture_output=True, text=True, check=False)
    assert result.returncode == 0 and not result.stdout.strip(), result.stdout + result.stderr


def test_the_metainfo_template_names_the_app_and_its_licence():
    text = METAINFO_TEMPLATE.read_text(encoding="utf-8")
    assert f"<id>{APP_ID}</id>" in text
    assert f'<launchable type="desktop-id">{PACKAGE}.desktop</launchable>' in text
    assert "<project_license>GPL-3.0-only</project_license>" in text
    assert '<release version="@VERSION@" date="@DATE@"/>' in text


@posix_only
@pytest.mark.skipif(shutil.which("appstreamcli") is None, reason="appstreamcli is not installed")
def test_the_metainfo_renders_and_validates(tmp_path):
    out = tmp_path / "out.metainfo.xml"
    result = subprocess.run(
        [bash(), str(RENDER_METAINFO), VERSION, str(out)],
        capture_output=True,
        text=True,
        env={**os.environ, "SOURCE_DATE_EPOCH": "1790000000"},
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f'<release version="{VERSION}" date="2026-09-21"/>' in out.read_text(encoding="utf-8")


def png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR"
    width, height = struct.unpack(">II", data[16:24])
    return width, height


@pytest.mark.parametrize("px", ICON_SIZES)
def test_each_bitmap_icon_has_its_size(px):
    assert png_size(PACKAGING / "icons" / f"{PACKAGE}-{px}.png") == (px, px)


# --- the .deb (nfpm) ----------------------------------------------------------------------------


def nfpm_entries() -> list[dict[str, str]]:
    """The ``contents`` entries of nfpm.yaml, as flat src/dst/type dicts (no YAML parser in the venv)."""
    entries: list[dict[str, str]] = []
    for line in NFPM.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^  (- |  )(src|dst|type): (.+)$", line)
        if match:
            if match.group(1) == "- ":
                entries.append({})
            entries[-1][match.group(2)] = match.group(3).strip()
    return entries


def test_the_deb_installs_the_bundle_under_opt_and_the_launcher_on_path():
    text = NFPM.read_text(encoding="utf-8")
    assert re.search(rf"^name: {PACKAGE}$", text, re.M)
    assert re.search(r'^license: "GPL-3.0-only"$', text, re.M)
    entries = nfpm_entries()
    by_dst = {entry["dst"]: entry for entry in entries}
    assert by_dst[f"/opt/{PACKAGE}/"] == {"src": f"dist/{APP_NAME}/", "dst": f"/opt/{PACKAGE}/", "type": "tree"}
    assert by_dst[f"/opt/{PACKAGE}/{PACKAGE}-launcher"]["src"] == "packaging/linux-launcher.sh"
    link = by_dst[f"/usr/bin/{COMMAND}"]
    assert link == {"src": f"/opt/{PACKAGE}/{PACKAGE}-launcher", "dst": f"/usr/bin/{COMMAND}", "type": "symlink"}
    assert by_dst[f"/usr/share/doc/{PACKAGE}/copyright"]["src"] == "LICENSE"
    assert by_dst[f"/usr/share/metainfo/{APP_ID}.metainfo.xml"]["src"] == f"dist/{PACKAGE}.metainfo.xml"
    assert by_dst[f"/usr/share/applications/{PACKAGE}.desktop"]["src"] == f"packaging/deb/{PACKAGE}.desktop"


def test_the_app_names_the_command_of_every_linux_package():
    # Only the .deb puts the command on PATH; the AppImage is its own file, and the tarball's
    # launcher sits in the extracted folder (the layout the tarball test above pins).
    assert COMMAND == "anki_miner_game"
    assert f"{COMMAND} for the .deb" in LINUX_CONTROL_NOTE
    assert "the full path of the .AppImage file" in LINUX_CONTROL_NOTE
    assert f"the full path of {APP_NAME}/{COMMAND}" in LINUX_CONTROL_NOTE
    assert "--toggle" in LINUX_CONTROL_NOTE
    launch = REPO / "anki_miner_game" / "launch.py"  # the entry point's own usage line
    assert f"``{COMMAND} [--arm <slug> | --start | --stop | --toggle]``" in launch.read_text(encoding="utf-8")


def test_every_deb_source_outside_dist_is_in_the_repo():
    for entry in nfpm_entries():
        src = entry["src"]
        if entry.get("type") != "symlink" and not src.startswith("dist/"):
            assert (REPO / src).is_file(), src


# --- the Windows installer ----------------------------------------------------------------------


def iss_setup() -> dict[str, str]:
    section = ISS.read_text(encoding="utf-8").split("[Setup]", 1)[1].split("\n[", 1)[0]
    return dict(line.split("=", 1) for line in section.splitlines() if "=" in line and not line.startswith(";"))


def test_the_windows_installer_packs_the_bundle_per_user():
    setup = iss_setup()
    assert re.fullmatch(r"\{\{[0-9A-F]{8}(-[0-9A-F]{4}){3}-[0-9A-F]{12}\}", setup["AppId"])
    assert setup["AppName"] == "Anki Miner Game"
    assert setup["PrivilegesRequired"] == "lowest"
    assert setup["OutputBaseFilename"] == f"{APP_NAME}-{{#AppVersion}}-Windows-x86_64-Setup"
    assert setup["UninstallDisplayIcon"] == f"{{app}}\\{APP_NAME}.exe"
    text = ISS.read_text(encoding="utf-8")
    assert f'Source: "..\\..\\dist\\{APP_NAME}\\*"; DestDir: "{{app}}"' in text
    assert 'Type: filesandordirs; Name: "{app}\\_internal"' in text  # no stale runtime across upgrades


def test_the_windows_installer_paths_resolve_from_its_folder():
    setup = iss_setup()
    for key in ("LicenseFile",):
        assert (ISS.parent / setup[key].replace("\\", "/")).resolve().is_file(), key
    assert (ISS.parent / setup["OutputDir"].replace("\\", "/")).resolve() == REPO / "dist"


def test_the_windows_uninstaller_says_where_the_user_data_stays():
    assert "%USERPROFILE%\\.anki_miner_game" in ISS.read_text(encoding="utf-8")


# --- shell scripts ------------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck is not installed")
def test_the_packaging_scripts_pass_shellcheck():
    scripts = sorted(str(p) for p in PACKAGING.rglob("*.sh"))
    assert len(scripts) == 4
    result = subprocess.run(["shellcheck", *scripts], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
