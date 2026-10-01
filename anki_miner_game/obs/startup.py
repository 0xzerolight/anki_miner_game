"""The one start-OBS sequence (D-03): OBS running and the gateway connected (spec 11.1, 17)."""

import asyncio
from collections.abc import Callable
from typing import Final

from anki_miner_game.interfaces.obs import ObsDiscovery, ObsGateway
from anki_miner_game.models.obs import ObsInfo, ObsNotReadyError, ObsServerOffError, ObsStartStage

OBS_LAUNCH_TIMEOUT_S: Final = 30.0
"""Spec 17: a launched OBS gets 30 s to answer ``GetVersion``."""


class LocalObsStarter:
    """``ObsStarter`` over the user's OBS. Holds no state between calls: the actor, the wizard and the
    picker may each use their own."""

    def __init__(
        self, discovery: ObsDiscovery, gateway: ObsGateway, *, timeout_s: float = OBS_LAUNCH_TIMEOUT_S
    ) -> None:
        self._discovery = discovery
        self._gateway = gateway
        self._timeout_s = timeout_s

    async def start(self, report: Callable[[ObsStartStage], None] | None = None) -> ObsInfo:
        tell = report if report is not None else _ignore
        if not await asyncio.to_thread(self._discovery.is_running):
            tell(ObsStartStage.ENABLING_SERVER)
            enabled = await asyncio.to_thread(self._discovery.ensure_server_enabled)
            if not enabled and await asyncio.to_thread(self._discovery.is_running):
                raise ObsServerOffError("OBS started meanwhile with its WebSocket server off")
            tell(ObsStartStage.LAUNCHING)
            await asyncio.to_thread(self._discovery.launch)  # ObsConnectError without an install
            if not await self._discovery.wait_ready(self._timeout_s):
                raise ObsNotReadyError(f"OBS did not answer within {self._timeout_s:g} s; an OBS dialog may be waiting")
        tell(ObsStartStage.CONNECTING)
        return await self._gateway.connect()


def _ignore(_stage: ObsStartStage) -> None:
    pass
