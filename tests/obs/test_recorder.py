"""The recorder (spec 11.4)."""

from typing import Any

import pytest

from anki_miner_game.interfaces.obs import Recorder
from anki_miner_game.models.obs import ObsRequestError
from anki_miner_game.obs.recorder import ObsRecorder


class Gateway:
    def __init__(self, error: Exception | None = None) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.error = error

    async def request(self, name: str, **fields: Any) -> dict[str, Any]:
        self.sent.append((name, fields))
        if self.error is not None:
            raise self.error
        return {}


async def test_start_and_stop_send_one_request_each():
    gateway = Gateway()
    recorder: Recorder = ObsRecorder(gateway)
    await recorder.start()
    await recorder.stop()
    assert gateway.sent == [("StartRecord", {}), ("StopRecord", {})]


async def test_obs_failure_reaches_the_caller():
    gateway = Gateway(ObsRequestError("StartRecord", 500, "Output already running"))
    with pytest.raises(ObsRequestError, match="500"):
        await ObsRecorder(gateway).start()


async def test_the_recorder_never_pauses():
    """The app never sends ``PauseRecord``: pause edges come only from OBS's own events (M0 ruling)."""
    assert not any(hasattr(ObsRecorder, name) for name in ("pause", "resume"))
