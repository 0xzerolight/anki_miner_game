"""tools/nested_display.py: env construction, bus config, owner guard and process-group teardown.

Nothing here starts kwin_wayland, a dbus-daemon or Xwayland. The teardown tests build a fake
child tree out of ``sleep`` and ``sh`` processes.
"""

import os
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tools import nested_display as nd

# The tool drives kwin, /proc and POSIX process groups; CI's Windows runners skip the module.
pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only tool")

TOKEN = "tok123"
BUS = "unix:path=/tmp/amg-12345678/bus,guid=abc"

# Everything a KDE Wayland session exports that must never reach a nested process.
OWNER_ENV = {
    "PATH": "/usr/bin:/bin",
    "LANG": "en_GB.UTF-8",
    "HOME": "/home/u",
    "DISPLAY": ":0",
    "WAYLAND_DISPLAY": "wayland-0",
    "XAUTHORITY": "/run/user/1000/xauth_owner",
    "XDG_SESSION_TYPE": "wayland",
    "XDG_CONFIG_HOME": "/home/u/.config",
    "XDG_CACHE_HOME": "/home/u/.cache",
    "XDG_DATA_HOME": "/home/u/.local/share",
    "XDG_STATE_HOME": "/home/u/.local/state",
    "XDG_RUNTIME_DIR": "/run/user/1000",
    "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
    "SESSION_MANAGER": "local/host:@/tmp/.ICE-unix/3070,unix/host:/tmp/.ICE-unix/3070",
    "AT_SPI_BUS_ADDRESS": "unix:path=/run/user/1000/at-spi/bus_0",
    "QT_QPA_PLATFORM": "wayland",
    "KDE_FULL_SESSION": "true",
    "SOMETHING_ELSE": "x",
}


def _xdg(tmp_path: Path) -> nd.XdgDirs:
    return nd.XdgDirs(tmp_path / "xdg", tmp_path / "run")


# --- XDG dirs and paths ---------------------------------------------------------------------


def test_xdg_dirs_are_private_and_the_runtime_dir_is_owner_only(tmp_path):
    xdg = _xdg(tmp_path)
    xdg.create()
    for path in (xdg.config, xdg.cache, xdg.data, xdg.state):
        assert path.is_dir()
        assert path.parent == xdg.root
    assert xdg.runtime.is_dir()
    assert (xdg.runtime.stat().st_mode & 0o777) == 0o700
    assert xdg.env() == {
        "XDG_CONFIG_HOME": str(xdg.config),
        "XDG_CACHE_HOME": str(xdg.cache),
        "XDG_DATA_HOME": str(xdg.data),
        "XDG_STATE_HOME": str(xdg.state),
        "XDG_RUNTIME_DIR": str(xdg.runtime),
    }


def test_the_default_xdg_root_lives_in_the_main_checkout_orchestration_data(tmp_path):
    main = tmp_path / "anki_miner_game"
    worktree = main / ".worktrees" / "r1-clock"
    assert nd.main_checkout(worktree) == main
    assert nd.main_checkout(main) == main
    assert nd.default_xdg_root("r1-clock", worktree) == main / ".orchestration" / "m0" / "data" / "r1-clock" / "xdg"


@pytest.mark.parametrize("caller", ["", "R1", "../x", "a b", "a/b"])
def test_the_caller_slug_is_validated(caller, tmp_path):
    with pytest.raises(nd.NestedDisplayError):
        nd.default_xdg_root(caller, tmp_path)


def test_an_xdg_root_outside_the_orchestration_data_dir_is_refused():
    # Short made-up paths: check_xdg_root never touches the filesystem, and a pytest tmp_path
    # under xdist is already too long for the socket-path check.
    base = Path("/amg/data")
    nd.check_xdg_root(base / "r1" / "xdg", base)
    with pytest.raises(nd.NestedDisplayError, match="outside"):
        nd.check_xdg_root(Path("/amg/elsewhere/xdg"), base)
    with pytest.raises(nd.NestedDisplayError, match="outside"):
        nd.check_xdg_root(base / ".." / "escape" / "xdg", base)


MAIN = Path("/home/light/Projects/anki_miner_game")
LONGEST_CALLER = "e1-m1-exit-linux"  # the longest slug the plan gives a nested display


def test_flatpaks_bus_proxy_socket_would_not_fit_under_the_orchestration_dirs():
    # Flatpak binds its bus proxy at realpath($XDG_RUNTIME_DIR)/.dbus-proxy/session-bus-proxy-XXXXXX.
    # Even the shortest caller's xdg root leaves no room for it, so the runtime dir lives elsewhere.
    for caller in ("r1-clock", LONGEST_CALLER):
        with pytest.raises(nd.NestedDisplayError, match="session-bus-proxy"):
            nd.check_runtime_dir(nd.default_xdg_root(caller, MAIN))


def test_the_runtime_dir_fits_flatpaks_bus_proxy_for_the_longest_caller(tmp_path):
    guard = nd.OwnerGuard(home=tmp_path, doc_path=Path("/run/user/1000/doc"), run=_Findmnt())
    display = nd.NestedDisplay(caller=LONGEST_CALLER, guard=guard, owner_env={"PATH": "/usr/bin", "HOME": "/h"})
    runtime = display.xdg.runtime
    assert display.xdg.root == nd.default_xdg_root(LONGEST_CALLER)  # config and state stay watched
    assert runtime.parent == Path("/tmp") and runtime.name.startswith("amg-")
    proxy = runtime / ".dbus-proxy" / "session-bus-proxy-XXXXXX"
    assert len(os.fsencode(str(proxy))) <= 107
    assert len(os.fsencode(str(proxy))) == len("/tmp/amg-12345678/.dbus-proxy/session-bus-proxy-XXXXXX")
    nd.check_runtime_dir(runtime)
    assert not runtime.exists()  # created at start, not by the constructor


def test_every_display_gets_its_own_runtime_dir(tmp_path):
    guard = nd.OwnerGuard(home=tmp_path, doc_path=Path("/run/user/1000/doc"), run=_Findmnt())
    env = {"PATH": "/usr/bin", "HOME": "/h"}
    first = nd.NestedDisplay(caller="r1-clock", guard=guard, owner_env=env)
    second = nd.NestedDisplay(caller="r1-clock", guard=guard, owner_env=env)
    assert first.xdg.runtime != second.xdg.runtime


def test_an_xdg_root_with_whitespace_is_refused(tmp_path):
    # kwin splits its positional application on whitespace.
    base = tmp_path / "data"
    with pytest.raises(nd.NestedDisplayError, match="whitespace"):
        nd.check_xdg_root(base / "r 1" / "xdg", base)


def test_a_runtime_dir_too_long_for_sun_path_is_refused():
    with pytest.raises(nd.NestedDisplayError, match="socket path"):
        nd.check_runtime_dir(Path("/tmp") / ("d" * 70))
    nd.check_runtime_dir(Path("/tmp") / ("d" * 60))


def test_creating_the_runtime_dir_refuses_one_that_already_exists(tmp_path):
    (tmp_path / "run").mkdir()
    with pytest.raises(FileExistsError):
        _xdg(tmp_path).create()


def test_the_runtime_dir_is_removed_with_what_the_session_left_in_it(tmp_path):
    xdg = _xdg(tmp_path)
    xdg.create()
    (xdg.runtime / ".flatpak" / "123").mkdir(parents=True)
    (xdg.runtime / ".flatpak" / "123" / "bwrapinfo.json").write_text("{}", encoding="utf-8")
    (xdg.runtime / "wayland-amg.lock").touch()
    nd.remove_runtime_dir(xdg.runtime, mountinfo="")
    assert not xdg.runtime.exists()
    assert xdg.config.is_dir()  # the watched dirs stay as evidence
    nd.remove_runtime_dir(xdg.runtime, mountinfo="")  # already gone: nothing to do


def test_a_runtime_dir_with_a_mount_inside_is_left_alone(tmp_path):
    # A document portal mounts at $XDG_RUNTIME_DIR/doc; deleting through it would delete the files
    # it exposes.
    xdg = _xdg(tmp_path)
    xdg.create()
    (xdg.runtime / "doc").mkdir()
    mountinfo = (
        "22 1 0:21 / /proc rw,nosuid - proc proc rw\n"
        f"99 22 0:77 / {xdg.runtime}/doc rw,nosuid,nodev - fuse.portal portal rw,user_id=1000\n"
    )
    with pytest.raises(nd.NestedDisplayError, match="doc"):
        nd.remove_runtime_dir(xdg.runtime, mountinfo=mountinfo)
    assert (xdg.runtime / "doc").is_dir()


def test_mount_points_are_read_with_their_octal_escapes_undone():
    mountinfo = "99 22 0:77 / /tmp/amg-1/a\\040b rw - tmpfs tmpfs rw\n"
    assert nd.mounts_under(Path("/tmp/amg-1"), mountinfo) == ["/tmp/amg-1/a b"]
    assert nd.mounts_under(Path("/tmp/amg-10"), mountinfo) == []


# --- the private bus --------------------------------------------------------------------------


def test_bus_config_has_no_service_activation_and_listens_on_the_private_socket(tmp_path):
    socket_path = tmp_path / "runtime" / "bus"
    text = nd.bus_config(socket_path)
    assert "servicedir" not in text  # neither <servicedir> nor <standard_session_servicedirs/>
    assert "<include" not in text  # session.d / session-local.conf could add service dirs
    assert "servicehelper" not in text
    root = ET.fromstring(text[text.index("<busconfig>") :])
    assert {child.tag for child in root} <= {"type", "listen", "auth", "policy", "keep_umask"}
    assert root.findtext("type") == "session"
    assert root.findtext("listen") == f"unix:path={socket_path}"


def test_dbus_argv_runs_the_daemon_in_the_foreground_from_the_custom_config(tmp_path):
    argv = nd.dbus_argv(tmp_path / "bus.conf", 7)
    assert argv[0] == "dbus-daemon"
    assert f"--config-file={tmp_path / 'bus.conf'}" in argv
    assert "--nofork" in argv
    assert "--print-address=7" in argv
    assert not any(arg.startswith(("--session", "--address", "--systemd")) for arg in argv)


# --- environment construction -----------------------------------------------------------------


def test_kwin_env_is_built_from_scratch(tmp_path):
    xdg = _xdg(tmp_path)
    env = nd.kwin_env(OWNER_ENV, xdg, BUS, TOKEN)
    assert env == {
        "PATH": "/usr/bin:/bin",
        "LANG": "en_GB.UTF-8",
        "HOME": "/home/u",
        **xdg.env(),
        "DBUS_SESSION_BUS_ADDRESS": BUS,
        nd.MARKER: TOKEN,
    }


def test_child_env_is_built_from_scratch_for_an_x11_client(tmp_path):
    xdg = _xdg(tmp_path)
    env = nd.child_env(OWNER_ENV, xdg, BUS, TOKEN, display=":7")
    assert env == {
        "PATH": "/usr/bin:/bin",
        "LANG": "en_GB.UTF-8",
        "HOME": "/home/u",
        **xdg.env(),
        "DBUS_SESSION_BUS_ADDRESS": BUS,
        nd.MARKER: TOKEN,
        "DISPLAY": ":7",
        "XDG_SESSION_TYPE": "x11",
        "QT_QPA_PLATFORM": "xcb",
        "GDK_BACKEND": "x11",
        "SDL_VIDEODRIVER": "x11",
        "FLATPAK_USER_DIR": "/home/u/.local/share/flatpak",
    }


@pytest.mark.parametrize(
    "key",
    ["SESSION_MANAGER", "AT_SPI_BUS_ADDRESS", "WAYLAND_DISPLAY", "XAUTHORITY", "KDE_FULL_SESSION", "SOMETHING_ELSE"],
)
def test_no_owner_session_variable_reaches_a_nested_process(key, tmp_path):
    xdg = _xdg(tmp_path)
    assert key not in nd.kwin_env(OWNER_ENV, xdg, BUS, TOKEN)
    assert key not in nd.child_env(OWNER_ENV, xdg, BUS, TOKEN, display=":7")


def test_no_owner_xdg_dir_or_bus_reaches_a_nested_process(tmp_path):
    xdg = _xdg(tmp_path)
    owner_values = {v for k, v in OWNER_ENV.items() if k.startswith(("XDG_", "DBUS"))}
    for env in (nd.kwin_env(OWNER_ENV, xdg, BUS, TOKEN), nd.child_env(OWNER_ENV, xdg, BUS, TOKEN, display=":7")):
        assert not owner_values & set(env.values())


def test_child_env_passes_a_reported_xauthority_and_caller_extras(tmp_path):
    env = nd.child_env(
        OWNER_ENV,
        _xdg(tmp_path),
        BUS,
        TOKEN,
        display=":7",
        xauthority="/x/runtime/xauth_1",
        extra={"PYTHONUNBUFFERED": "1"},
    )
    assert env["XAUTHORITY"] == "/x/runtime/xauth_1"
    assert env["PYTHONUNBUFFERED"] == "1"


@pytest.mark.parametrize(
    "key", ["DBUS_SESSION_BUS_ADDRESS", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR", "WAYLAND_DISPLAY", "SESSION_MANAGER"]
)
def test_caller_extras_cannot_reopen_the_owner_session(key, tmp_path):
    with pytest.raises(nd.NestedDisplayError, match=key):
        nd.child_env(OWNER_ENV, _xdg(tmp_path), BUS, TOKEN, display=":7", extra={key: "x"})


def test_env_defaults_lang_and_flatpak_dir_when_the_owner_env_lacks_them(tmp_path):
    env = nd.child_env({"PATH": "/usr/bin", "HOME": "/home/u"}, _xdg(tmp_path), BUS, TOKEN, display=":7")
    assert env["LANG"] == "C.UTF-8"
    assert env["FLATPAK_USER_DIR"] == "/home/u/.local/share/flatpak"


def test_env_construction_does_not_mutate_the_owner_env(tmp_path):
    before = dict(OWNER_ENV)
    nd.child_env(OWNER_ENV, _xdg(tmp_path), BUS, TOKEN, display=":7")
    nd.kwin_env(OWNER_ENV, _xdg(tmp_path), BUS, TOKEN)
    assert before == OWNER_ENV


# --- kwin, reporter, rootful Xwayland, flatpak ------------------------------------------------


def test_kwin_argv_is_a_virtual_headless_session_with_xwayland():
    argv = nd.kwin_argv("wayland-amg", 1280, 720, "/d/xdg/report.sh")
    assert argv[0] == "kwin_wayland"
    for flag in ("--virtual", "--xwayland", "--no-lockscreen", "--no-global-shortcuts", "--no-kactivities"):
        assert flag in argv
    assert argv[argv.index("--socket") + 1] == "wayland-amg"
    assert argv[argv.index("--width") + 1] == "1280"
    assert argv[argv.index("--height") + 1] == "720"
    assert argv[-1] == "/d/xdg/report.sh"


def test_parse_report_reads_display_and_xauthority():
    assert nd.parse_report("DISPLAY=:3\nXAUTHORITY=/r/xauth_q\n") == (":3", "/r/xauth_q")
    assert nd.parse_report("DISPLAY=:3\nXAUTHORITY=\n") == (":3", None)


def test_parse_report_rejects_a_malformed_display():
    with pytest.raises(nd.NestedDisplayError, match="unexpected DISPLAY"):
        nd.parse_report("DISPLAY=wayland-0\n")


def test_reporter_script_writes_display_and_xauthority_atomically(tmp_path):
    report = tmp_path / "display.env"
    script = tmp_path / "report.sh"
    text = nd.reporter_script(report)
    assert text.startswith("#!/bin/sh\n")
    assert "mv" in text  # written to a temp name, then moved into place
    script.write_text(text, encoding="utf-8")
    script.chmod(0o755)
    subprocess.run([str(script)], env={"PATH": "/usr/bin:/bin", "DISPLAY": ":9", "XAUTHORITY": "/a"}, check=True)
    assert nd.parse_report(report.read_text(encoding="utf-8")) == (":9", "/a")
    assert set(tmp_path.iterdir()) == {report, script}  # no temp file left behind


def test_wait_for_report_returns_the_display(tmp_path):
    report = tmp_path / "display.env"
    report.write_text("DISPLAY=:3\nXAUTHORITY=\n", encoding="utf-8")
    assert nd.wait_for_report(report, alive=lambda: True, timeout_s=1) == (":3", None)


def test_wait_for_report_fails_when_kwin_dies(tmp_path):
    with pytest.raises(nd.NestedDisplayError, match="exited"):
        nd.wait_for_report(tmp_path / "display.env", alive=lambda: False, timeout_s=5)


def test_wait_for_report_times_out(tmp_path):
    with pytest.raises(nd.NestedDisplayError, match="no display"):
        nd.wait_for_report(tmp_path / "display.env", alive=lambda: True, timeout_s=0.2, poll_s=0.05)


def test_rootful_xwayland_argv_reports_its_display_on_a_pipe():
    argv = nd.rootful_xwayland_argv(1280, 720, 9)
    assert argv[0] == "Xwayland"
    assert argv[argv.index("-displayfd") + 1] == "9"
    assert argv[argv.index("-geometry") + 1] == "1280x720"
    assert "-rootless" not in argv


def test_flatpak_x11_argv_withholds_wayland_and_the_owner_portals():
    argv = nd.flatpak_x11_argv("com.obsproject.Studio", "--minimize-to-tray")
    assert argv[:2] == ["flatpak", "run"]
    for flag in (
        "--nosocket=wayland",
        "--socket=x11",
        "--no-documents-portal",
        "--no-a11y-bus",
        "--die-with-parent",
        "--env=QT_QPA_PLATFORM=xcb",
    ):
        assert flag in argv
    assert argv[-2:] == ["com.obsproject.Studio", "--minimize-to-tray"]


# --- owner guard ----------------------------------------------------------------------------


def _owner_home(tmp_path: Path) -> Path:
    home = tmp_path / "owner"
    for rel in nd.GUARDED_CONFIG_FILES:
        path = home / ".config" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
    return home


class _Findmnt:
    """Fake ``findmnt`` runner: reports ``fstype`` for the doc mount (None = not mounted)."""

    def __init__(self, fstype: str | None = "fuse.portal") -> None:
        self.fstype = fstype
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str]:
        self.calls.append(argv)
        return (1, "") if self.fstype is None else (0, self.fstype + "\n")


def _guard(tmp_path: Path, findmnt: _Findmnt) -> nd.OwnerGuard:
    return nd.OwnerGuard(home=_owner_home(tmp_path), doc_path=Path("/run/user/1000/doc"), run=findmnt)


def test_the_guard_covers_the_kwin_and_toolkit_files_the_incidents_touched():
    for rel in ("kwinrc", "kwinoutputconfig.json", "kglobalshortcutsrc", "gtkrc", "Trolltech.conf"):
        assert rel in nd.GUARDED_CONFIG_FILES


def test_the_guard_asks_findmnt_about_the_doc_mount(tmp_path):
    findmnt = _Findmnt()
    assert _guard(tmp_path, findmnt).fingerprint().doc_fstype == "fuse.portal"
    assert findmnt.calls[-1] == ["findmnt", "-n", "-o", "FSTYPE", "--mountpoint", "/run/user/1000/doc"]


def test_an_unchanged_owner_passes_the_guard(tmp_path):
    guard = _guard(tmp_path, _Findmnt())
    guard.arm()
    guard.check()
    assert guard.changes() == []


def test_the_guard_refuses_to_arm_without_the_portal_mount(tmp_path):
    with pytest.raises(nd.IsolationBreachError, match="fuse.portal"):
        _guard(tmp_path, _Findmnt(fstype=None)).arm()


def test_the_guard_reports_a_rewritten_owner_file(tmp_path):
    guard = _guard(tmp_path, _Findmnt())
    guard.arm()
    kwinrc = guard.home / ".config" / "kwinrc"
    st = kwinrc.stat()
    os.utime(kwinrc, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    with pytest.raises(nd.IsolationBreachError, match="kwinrc"):
        guard.check()


def test_the_guard_reports_a_new_owner_file(tmp_path):
    guard = _guard(tmp_path, _Findmnt())
    (guard.home / ".config" / "kwinrc").unlink()
    guard.arm()
    (guard.home / ".config" / "kwinrc").write_text("new", encoding="utf-8")
    assert any("kwinrc" in change for change in guard.changes())


def test_the_guard_reports_a_lost_portal_mount(tmp_path):
    findmnt = _Findmnt()
    guard = _guard(tmp_path, findmnt)
    guard.arm()
    findmnt.fstype = None
    with pytest.raises(nd.IsolationBreachError, match="/run/user/1000/doc"):
        guard.check()


def test_the_guard_needs_arming_before_a_check(tmp_path):
    with pytest.raises(nd.NestedDisplayError, match="arm"):
        _guard(tmp_path, _Findmnt()).check()


def test_fingerprints_round_trip_through_json(tmp_path):
    fp = _guard(tmp_path, _Findmnt()).fingerprint()
    assert nd.OwnerFingerprint.from_json(fp.to_json()) == fp


# --- process-group teardown with a fake child tree ------------------------------------------


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


def _wait_for_pid_file(path: Path, timeout_s: float = 5.0) -> int:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        text = path.read_text(encoding="utf-8").strip() if path.exists() else ""
        if text:
            return int(text.split()[0])
        time.sleep(0.02)
    raise AssertionError(f"no pid in {path}")


PLAIN_ENV = {"PATH": "/usr/bin:/bin"}


def test_spawned_children_join_the_leader_process_group():
    group = nd.ProcessGroup(f"{TOKEN}-join-{os.getpid()}")
    try:
        leader = group.spawn(["sleep", "60"], env=PLAIN_ENV)
        child = group.spawn(["sleep", "60"], env=PLAIN_ENV)
        assert group.pgid == leader.pid
        assert os.getpgid(leader.pid) == leader.pid
        assert os.getpgid(child.pid) == leader.pid
        assert os.getpgid(0) != leader.pid  # the caller stays out of the group
        assert set(nd.group_members(leader.pid)) == {leader.pid, child.pid}
    finally:
        assert group.terminate(term_wait_s=2, kill_wait_s=2) == []


def test_teardown_kills_the_whole_tree_including_term_ignorers_and_session_escapees(tmp_path):
    token = f"{TOKEN}-tree-{os.getpid()}"
    group = nd.ProcessGroup(token)
    env = {**PLAIN_ENV, nd.MARKER: token}
    grandchild_file = tmp_path / "grandchild.pid"
    stubborn_file = tmp_path / "stubborn.pid"
    escapee_file = tmp_path / "escapee.pid"
    # A leader (stands in for dbus-daemon), a child with a grandchild (kwin -> Xwayland), a
    # child whose tree ignores SIGTERM, and a grandchild that leaves the group with setsid.
    leader = group.spawn(["sleep", "60"], env=env)
    parent = group.spawn(["sh", "-c", f'sleep 60 & echo $! > "{grandchild_file}"; wait'], env=env)
    stubborn = group.spawn(["sh", "-c", f"trap '' TERM; sleep 60 & echo $! > \"{stubborn_file}\"; wait"], env=env)
    escaper = group.spawn(["sh", "-c", f'setsid sleep 60 & echo $! > "{escapee_file}"; wait'], env=env)
    grandchild = _wait_for_pid_file(grandchild_file)
    stubborn_child = _wait_for_pid_file(stubborn_file)
    escapee = _wait_for_pid_file(escapee_file)
    deadline = time.monotonic() + 5
    while os.getsid(escapee) != escapee and time.monotonic() < deadline:
        time.sleep(0.02)
    assert os.getpgid(escapee) != group.pgid  # precondition: killpg alone would miss it
    pids = [leader.pid, parent.pid, grandchild, stubborn.pid, stubborn_child, escaper.pid, escapee]

    survivors = group.terminate(term_wait_s=0.5, kill_wait_s=3)

    assert survivors == []
    assert [pid for pid in pids if _alive(pid)] == []


def test_teardown_of_an_empty_group_is_a_no_op():
    assert nd.ProcessGroup(TOKEN + "-empty").terminate() == []


def test_stopping_a_display_removes_its_runtime_dir(tmp_path):
    guard = nd.OwnerGuard(home=tmp_path, doc_path=Path("/run/user/1000/doc"), run=_Findmnt())
    display = nd.NestedDisplay(
        xdg_root=tmp_path / "data" / "xdg", runtime_base=tmp_path, guard=guard, owner_env=PLAIN_ENV
    )
    display.xdg.create()
    (display.xdg.runtime / "bus").touch()
    display.stop(term_wait_s=1)
    assert not display.xdg.runtime.exists()
    assert display.xdg.config.is_dir()


def test_a_failed_start_never_removes_a_runtime_dir_it_did_not_create(tmp_path, monkeypatch):
    monkeypatch.setattr(nd, "check_runtime_dir", lambda runtime: None)  # tmp_path is too long for sun_path
    guard = nd.OwnerGuard(home=tmp_path, doc_path=Path("/run/user/1000/doc"), run=_Findmnt())
    display = nd.NestedDisplay(
        xdg_root=tmp_path / "data" / "xdg",
        data_base=tmp_path / "data",
        runtime_base=tmp_path,
        guard=guard,
        owner_env=PLAIN_ENV,
    )
    display.xdg.runtime.mkdir()
    (display.xdg.runtime / "someone-elses").touch()
    with pytest.raises(FileExistsError):
        display.start()
    assert (display.xdg.runtime / "someone-elses").exists()


def test_marker_holders_are_exactly_the_marked_processes():
    token = f"{TOKEN}-holders-{os.getpid()}"
    group = nd.ProcessGroup(token)
    try:
        marked = group.spawn(["sleep", "60"], env={**PLAIN_ENV, nd.MARKER: token})
        other = group.spawn(["sleep", "60"], env={**PLAIN_ENV, nd.MARKER: token + "-other"})
        holders = nd.marker_holders(token)
        assert marked.pid in holders
        assert other.pid not in holders
        assert os.getpid() not in holders
    finally:
        group.terminate(term_wait_s=2, kill_wait_s=2)


# --- CLI ------------------------------------------------------------------------------------


def test_cli_refuses_run_without_a_command():
    with pytest.raises(SystemExit) as exc:
        nd.main(["run", "--caller", "r1"])
    assert exc.value.code == 2


def test_cli_refuses_setenv_without_an_equals_sign():
    with pytest.raises(SystemExit) as exc:
        nd.main(["run", "--caller", "r1", "--setenv", "FOO", "--", "true"])
    assert exc.value.code == 2


def test_cli_reports_missing_tools(monkeypatch, capsys):
    monkeypatch.setattr(nd.shutil, "which", lambda name: None)
    assert nd.main(["run", "--caller", "r1", "--", "true"]) == 2
    err = capsys.readouterr().err
    assert "kwin_wayland" in err and "dbus-daemon" in err


def test_cli_guard_saves_and_compares_a_baseline(tmp_path, monkeypatch, capsys):
    home = _owner_home(tmp_path)
    monkeypatch.setattr(nd, "_findmnt", _Findmnt())
    baseline = tmp_path / "baseline.json"
    assert nd.main(["guard", "--home", str(home), "--save", str(baseline)]) == 0
    assert nd.main(["guard", "--home", str(home), "--baseline", str(baseline)]) == 0
    assert "unchanged" in capsys.readouterr().out
    trolltech = home / ".config" / "Trolltech.conf"
    st = trolltech.stat()
    os.utime(trolltech, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000))
    assert nd.main(["guard", "--home", str(home), "--baseline", str(baseline)]) == nd.EXIT_BREACH
    err = capsys.readouterr().err
    assert "CRITICAL" in err and "Trolltech.conf" in err


def test_teardown_signals_are_term_then_kill():
    assert nd.TEARDOWN_SIGNALS == (signal.SIGTERM, signal.SIGKILL)


def test_cli_refuses_a_protected_setenv_before_starting_anything(monkeypatch, capsys):
    monkeypatch.setattr(nd.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(nd, "NestedDisplay", None)  # constructing one would fail the test
    assert nd.main(["run", "--caller", "r1", "--setenv", "WAYLAND_DISPLAY=wayland-0", "--", "true"]) == 2
    assert "WAYLAND_DISPLAY" in capsys.readouterr().err


def test_a_relative_xdg_root_becomes_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    guard = nd.OwnerGuard(home=tmp_path, doc_path=Path("/run/user/1000/doc"), run=_Findmnt())
    display = nd.NestedDisplay(xdg_root=Path("rel/xdg"), guard=guard, owner_env={"PATH": "/usr/bin", "HOME": "/h"})
    assert display.xdg.root == tmp_path / "rel" / "xdg"
