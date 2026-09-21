"""The recorder (spec 11.4): ``StartRecord`` and ``StopRecord``, nothing else.

It changes no state itself. The session actor follows ``RecordStateChanged`` events (spec 6), so a
recording started from the OBS window, a hotkey or this recorder behaves the same, and the actor
alone decides when a start is allowed (only while ``armed``). The app never pauses a recording:
pause edges come only from OBS's own ``PAUSED`` / ``RESUMED`` events (M0 ruling on R1 finding 1).
"""

from anki_miner_game.interfaces.obs import ObsGateway


class ObsRecorder:
    """``Recorder`` over an ``ObsGateway``. Errors (``ObsError``) reach the caller unchanged."""

    def __init__(self, gateway: ObsGateway) -> None:
        self._gateway = gateway

    async def start(self) -> None:
        """``StartRecord``. OBS answers once the start is queued, before ``STARTING`` (source findings 18)."""
        await self._gateway.request("StartRecord")

    async def stop(self) -> None:
        await self._gateway.request("StopRecord")
