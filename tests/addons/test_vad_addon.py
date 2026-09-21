"""``addons/vad_addon.py``: the VAD add-on environment (spec 13.1, 17 VAD row).

Every test but the ``network`` + ``vad`` one drives ``VadAddon`` with a fake ``ensure_uv``, a fake
``uv`` runner that only records its command lines (and creates the venv's interpreter file the way
``uv venv`` would), and an injected transport serving a small stand-in model with its own pin.
"""

import asyncio
import contextlib
import hashlib
import platform
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from anki_miner_game.addons import bootstrap
from anki_miner_game.addons.bootstrap import BootstrapError, Reply, uv_environment
from anki_miner_game.addons.vad_addon import (
    MODEL,
    MODEL_HOSTS,
    PYTHON_VERSION,
    REQUIREMENTS,
    ModelPin,
    VadAddon,
    VadAddonError,
)
from anki_miner_game.models.addons import AddonStatus
from anki_miner_game.vad import model_pin

MODEL_BYTES = b"not really onnx, " * 64
MODEL_URL = "https://raw.githubusercontent.com/SYSTRAN/faster-whisper/abc/faster_whisper/assets/silero_vad_v6.onnx"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


FAKE_MODEL = ModelPin(url=MODEL_URL, sha256=_sha(MODEL_BYTES), size=len(MODEL_BYTES), filename="silero_vad_v6.onnx")


def ok(data: bytes, *, length: int | None = -1, chunk: int = 100) -> Callable[[], Reply]:
    """A 200 reply factory; ``length=-1`` means the true length."""

    def make() -> Reply:
        chunks = (data[i : i + chunk] for i in range(0, len(data), chunk))
        return Reply(status=200, location=None, length=len(data) if length == -1 else length, chunks=chunks)

    return make


def status_reply(status: int, location: str | None = None) -> Callable[[], Reply]:
    return lambda: Reply(status=status, location=location, length=0, chunks=iter(()))


class FakeTransport:
    """Serves one reply factory per URL and records every URL asked for."""

    def __init__(self, routes: dict[str, Callable[[], Reply]]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    @contextlib.contextmanager
    def __call__(self, url: str) -> Iterator[Reply]:
        self.calls.append(url)
        if url not in self.routes:
            raise AssertionError(f"unexpected request for {url}")
        yield self.routes[url]()


class FakeUv:
    """Stands in for ``subprocess.run`` on the uv binary: records each call; ``uv venv`` creates the
    venv's interpreter file. ``fail`` names a subcommand (``venv`` or ``pip``) that exits 2."""

    def __init__(self, fail: str | None = None, during: Callable[[], None] | None = None) -> None:
        self.fail = fail
        self.during = during
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, cmd, **kwargs) -> subprocess.CompletedProcess[str]:
        cmd = [str(part) for part in cmd]
        self.calls.append((cmd, kwargs))
        if self.during is not None:
            self.during()
        if cmd[1] == self.fail:
            return subprocess.CompletedProcess(cmd, 2, stdout="", stderr="error: no route to the index\n")
        if cmd[1] == "venv":
            python = Path(cmd[-1]) / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
            python.parent.mkdir(parents=True)
            python.write_bytes(b"")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    @property
    def commands(self) -> list[list[str]]:
        return [cmd for cmd, _ in self.calls]


class FakeEnsureUv:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    def __call__(self, home: Path, *, progress=None) -> Path:
        self.calls += 1
        if self.error is not None:
            raise self.error
        if progress is not None:
            progress(50, 100)
            progress(100, 100)
        return home / "bin" / "uv"


def make_addon(
    home: Path,
    *,
    uv: FakeUv | None = None,
    ensure: FakeEnsureUv | None = None,
    transport: FakeTransport | None = None,
    model: ModelPin = FAKE_MODEL,
) -> VadAddon:
    return VadAddon(
        home,
        model=model,
        run=uv or FakeUv(),
        ensure_uv=ensure or FakeEnsureUv(),
        transport=transport or FakeTransport({model.url: ok(MODEL_BYTES)}),
    )


def install(addon: VadAddon, progress: Callable[[int, int], None] | None = None) -> None:
    asyncio.run(addon.install(progress or (lambda done, total: None)))


def fake_install(addon: VadAddon, model_bytes: bytes = MODEL_BYTES) -> None:
    """What a finished install leaves on disk, made by hand."""
    addon.python_path.parent.mkdir(parents=True)
    addon.python_path.write_bytes(b"")
    addon.model_path.write_bytes(model_bytes)


# --- pins and layout --------------------------------------------------------


def test_the_model_pin_is_model_pin_py():
    assert (
        ModelPin(
            url=model_pin.MODEL_URL,
            sha256=model_pin.MODEL_SHA256,
            size=model_pin.MODEL_SIZE_BYTES,
            filename=model_pin.MODEL_FILENAME,
        )
        == MODEL
    )
    assert frozenset({"raw.githubusercontent.com"}) == MODEL_HOSTS


def test_the_requirements_are_the_workers_pinned_file():
    assert (
        Path(__file__).resolve().parents[2] / "anki_miner_game" / "vad" / "worker" / "requirements.txt" == REQUIREMENTS
    )
    assert REQUIREMENTS.is_file()
    assert PYTHON_VERSION == "3.12"


def test_everything_lives_under_the_addon_folder(tmp_path):
    addon = make_addon(tmp_path)

    assert addon.root == tmp_path / "addons" / "vad"
    assert addon.model_path == addon.root / "env" / "silero_vad_v6.onnx"
    assert addon.python_path.is_relative_to(addon.root / "env" / "venv")


def test_size_counts_uv_the_environment_and_the_model(tmp_path):
    size = make_addon(tmp_path, model=MODEL).size_bytes
    uv_pin = bootstrap.pin_for(sys.platform, platform.machine())

    assert size > MODEL.size + (uv_pin.size if uv_pin else 0) + 50_000_000
    assert size < 250_000_000


# --- status -----------------------------------------------------------------


def test_status_is_missing_before_any_install(tmp_path):
    assert make_addon(tmp_path).status() is AddonStatus.MISSING


def test_status_is_ready_when_the_interpreter_and_the_pinned_model_are_there(tmp_path):
    addon = make_addon(tmp_path)
    fake_install(addon)

    assert addon.status() is AddonStatus.READY


@pytest.mark.parametrize("damage", ["model bytes", "model missing", "interpreter missing"])
def test_status_is_broken_when_the_installed_files_fail_verification(tmp_path, damage):
    addon = make_addon(tmp_path)
    fake_install(addon)
    if damage == "model bytes":
        addon.model_path.write_bytes(MODEL_BYTES[:-1] + b"!")
    elif damage == "model missing":
        addon.model_path.unlink()
    else:
        addon.python_path.unlink()

    assert addon.status() is AddonStatus.BROKEN


# --- install ----------------------------------------------------------------


def test_install_builds_the_venv_from_the_pinned_requirements_and_fetches_the_model(tmp_path):
    uv = FakeUv()
    transport = FakeTransport({FAKE_MODEL.url: ok(MODEL_BYTES)})
    addon = make_addon(tmp_path, uv=uv, transport=transport)

    install(addon)

    uv_exe = str(tmp_path / "bin" / "uv")
    venv = addon.root / "env" / "venv"
    assert uv.commands == [
        [uv_exe, "venv", "--no-project", "--python", PYTHON_VERSION, str(venv)],
        [
            uv_exe,
            "pip",
            "install",
            "--python",
            str(addon.python_path),
            "--require-hashes",
            "--only-binary",
            ":all:",
            "-r",
            str(REQUIREMENTS),
        ],
    ]
    assert transport.calls == [FAKE_MODEL.url]
    assert addon.model_path.read_bytes() == MODEL_BYTES
    assert addon.status() is AddonStatus.READY
    assert sorted(p.name for p in (addon.root / "env").iterdir()) == ["silero_vad_v6.onnx", "venv"]


def test_every_uv_call_runs_in_the_addons_own_uv_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("AMG_TEST_MARKER", "kept")
    uv = FakeUv()
    addon = make_addon(tmp_path, uv=uv)

    install(addon)

    for _, kwargs in uv.calls:
        env = kwargs["env"]
        assert env.items() >= uv_environment(tmp_path, "vad").items()
        assert env["AMG_TEST_MARKER"] == "kept"  # the rest of the environment passes through
        assert kwargs["cwd"] == addon.root
        assert kwargs.get("check", False) is False


def test_progress_runs_from_the_uv_download_to_the_total_without_going_back(tmp_path):
    seen: list[tuple[int, int]] = []
    addon = make_addon(tmp_path)

    install(addon, lambda done, total: seen.append((done, total)))

    assert seen[-1] == (addon.size_bytes, addon.size_bytes)
    assert {total for _, total in seen} == {addon.size_bytes}
    assert [done for done, _ in seen] == sorted(done for done, _ in seen)
    assert len(seen) > 4  # uv download, environment, model chunks, end


def test_install_does_nothing_when_already_ready(tmp_path):
    uv, ensure = FakeUv(), FakeEnsureUv()
    addon = make_addon(tmp_path, uv=uv, ensure=ensure)
    fake_install(addon)

    install(addon)

    assert uv.calls == [] and ensure.calls == 0


def test_install_repairs_a_broken_install(tmp_path):
    addon = make_addon(tmp_path)
    fake_install(addon, model_bytes=b"damaged")
    stray = addon.root / "env" / "venv" / "lib" / "stray.py"
    stray.parent.mkdir(parents=True)
    stray.write_text("x")
    assert addon.status() is AddonStatus.BROKEN

    install(addon)

    assert addon.status() is AddonStatus.READY
    assert not stray.exists()


def test_status_is_installing_while_an_install_runs(tmp_path):
    seen: list[AddonStatus] = []
    addon: VadAddon

    def during() -> None:
        seen.append(addon.status())

    addon = make_addon(tmp_path, uv=FakeUv(during=during))
    install(addon)

    assert seen == [AddonStatus.INSTALLING, AddonStatus.INSTALLING]
    assert addon.status() is AddonStatus.READY


def test_two_installs_at_once_build_the_environment_once(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    def during() -> None:
        entered.set()
        release.wait(10)

    uv = FakeUv(during=during)
    addon = make_addon(tmp_path, uv=uv)

    async def both() -> None:
        first = asyncio.create_task(addon.install(lambda d, t: None))
        await asyncio.to_thread(entered.wait, 10)
        second = asyncio.create_task(addon.install(lambda d, t: None))
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.gather(first, second)

    asyncio.run(both())

    assert [cmd[1] for cmd in uv.commands] == ["venv", "pip"]
    assert addon.status() is AddonStatus.READY


# --- install failures: always VadAddonError, nothing left behind -------------


def assert_nothing_installed(addon: VadAddon) -> None:
    assert addon.status() is AddonStatus.MISSING
    assert not (addon.root / "env").exists()


@pytest.mark.parametrize("step", ["venv", "pip"])
def test_a_failing_uv_step_names_uv_and_its_error(tmp_path, step):
    addon = make_addon(tmp_path, uv=FakeUv(fail=step))

    with pytest.raises(VadAddonError, match=f"uv {step}.*exit 2.*no route to the index"):
        install(addon)

    assert_nothing_installed(addon)


def test_a_uv_that_cannot_start_is_a_vad_addon_error(tmp_path):
    def run(cmd, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", str(cmd[0]))

    addon = VadAddon(tmp_path, model=FAKE_MODEL, run=run, ensure_uv=FakeEnsureUv(), transport=FakeTransport({}))

    with pytest.raises(VadAddonError, match="No such file"):
        install(addon)

    assert_nothing_installed(addon)


def test_a_bootstrap_failure_is_a_vad_addon_error(tmp_path):
    uv = FakeUv()
    addon = make_addon(tmp_path, uv=uv, ensure=FakeEnsureUv(BootstrapError("uv download checksum mismatch")))

    with pytest.raises(VadAddonError, match="uv download checksum mismatch"):
        install(addon)

    assert uv.calls == []
    assert_nothing_installed(addon)


def test_a_model_with_the_wrong_bytes_is_refused(tmp_path):
    other = b"x" * len(MODEL_BYTES)
    addon = make_addon(tmp_path, transport=FakeTransport({FAKE_MODEL.url: ok(other)}))

    with pytest.raises(VadAddonError, match="checksum"):
        install(addon)

    assert_nothing_installed(addon)


@pytest.mark.parametrize("length", [len(MODEL_BYTES) + 1, None], ids=["stated", "streamed"])
def test_a_model_larger_than_the_pin_is_refused(tmp_path, length):
    body = MODEL_BYTES + b"more"
    addon = make_addon(tmp_path, transport=FakeTransport({FAKE_MODEL.url: ok(body, length=length)}))

    with pytest.raises(VadAddonError, match="larger"):
        install(addon)

    assert_nothing_installed(addon)


@pytest.mark.parametrize(
    "url",
    [
        "http://raw.githubusercontent.com/o/r/c/m.onnx",
        "https://example.com/m.onnx",
        "https://raw.githubusercontent.com.example.com/m.onnx",
        "https://user@evil.example/raw.githubusercontent.com/m.onnx",
    ],
)
def test_a_model_url_off_https_or_off_the_allowed_host_is_never_requested(tmp_path, url):
    transport = FakeTransport({})
    pin = ModelPin(url=url, sha256=FAKE_MODEL.sha256, size=FAKE_MODEL.size, filename=FAKE_MODEL.filename)
    addon = make_addon(tmp_path, transport=transport, model=pin)

    with pytest.raises(VadAddonError, match="refusing"):
        install(addon)

    assert transport.calls == []
    assert_nothing_installed(addon)


def test_a_redirect_on_the_allowed_host_is_followed(tmp_path):
    moved = "https://raw.githubusercontent.com/SYSTRAN/faster-whisper/def/silero_vad_v6.onnx"
    transport = FakeTransport({FAKE_MODEL.url: status_reply(302, moved), moved: ok(MODEL_BYTES)})
    addon = make_addon(tmp_path, transport=transport)

    install(addon)

    assert transport.calls == [FAKE_MODEL.url, moved]
    assert addon.status() is AddonStatus.READY


def test_a_redirect_off_the_allowed_host_is_not_followed(tmp_path):
    transport = FakeTransport({FAKE_MODEL.url: status_reply(302, "https://example.com/m.onnx")})
    addon = make_addon(tmp_path, transport=transport)

    with pytest.raises(VadAddonError, match="refusing"):
        install(addon)

    assert transport.calls == [FAKE_MODEL.url]
    assert_nothing_installed(addon)


def test_an_http_error_status_is_refused(tmp_path):
    addon = make_addon(tmp_path, transport=FakeTransport({FAKE_MODEL.url: status_reply(404)}))

    with pytest.raises(VadAddonError, match="404"):
        install(addon)

    assert_nothing_installed(addon)


def test_a_transport_failure_is_a_vad_addon_error(tmp_path):
    @contextlib.contextmanager
    def transport(url: str) -> Iterator[Reply]:
        raise OSError("connection reset")
        yield  # pragma: no cover

    addon = VadAddon(tmp_path, model=FAKE_MODEL, run=FakeUv(), ensure_uv=FakeEnsureUv(), transport=transport)

    with pytest.raises(VadAddonError, match="connection reset"):
        install(addon)

    assert_nothing_installed(addon)


def test_a_failed_reinstall_leaves_no_broken_files_either(tmp_path):
    addon = make_addon(tmp_path, uv=FakeUv(fail="pip"))
    fake_install(addon, model_bytes=b"damaged")

    with pytest.raises(VadAddonError):
        install(addon)

    assert_nothing_installed(addon)
