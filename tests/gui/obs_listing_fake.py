"""T14's ``FakeObs`` plus what the window picker's listing needs: output status requests and events.

- ``GetStreamStatus``, ``GetRecordStatus``, ``GetReplayBufferStatus``, ``GetVirtualCamStatus``
  answer ``outputActive``; the replay buffer and the virtual camera answer 604 when not configured
  or not installed, as OBS 32.2.2 did in ``arm_disarm.jsonl`` (``docs/m0/obs-behaviour.md`` item 6).
- ``FakeObs`` sends a switch's ``...Changing`` and ``...Changed`` events itself. A switch to another
  collection than the app's (the picker's switch back) sends them per ``changed_event``: before its
  answer (``"before"``, the usual order), from another thread ``event_delay_s`` after its answer
  (``"after"``, seen twice in R2, item 5), or never (``"never"``). The provisioner's own switch to the
  app's collection always sends them before its answer. Switching to the current collection sends no
  event (R2 item 4).
"""

import json
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from anki_miner_game.models.constants import OBS_COLLECTION_NAME
from anki_miner_game.models.obs import ObsEventName, ObsRequestError
from tests.obs.fake_obs import FakeObs

TRANSCRIPTS = Path(__file__).resolve().parents[1] / "fixtures" / "obs_transcripts"

STATUS_REQUESTS = ("GetStreamStatus", "GetRecordStatus", "GetReplayBufferStatus", "GetVirtualCamStatus")
NOT_AVAILABLE = 604
"""obs-websocket ``RequestStatus::InvalidResourceState``: that output is not configured or not installed."""


def recorded_window_lists(transcript: str) -> list[list[dict[str, Any]]]:
    """Every ``propertyItems`` a real OBS answered to ``GetInputPropertiesListPropertyItems`` in ``transcript``."""
    lists: list[list[dict[str, Any]]] = []
    for line in (TRANSCRIPTS / transcript).read_text(encoding="utf-8").splitlines():
        msg = json.loads(line).get("msg") or {}
        data = msg.get("d") or {}
        if msg.get("op") == 7 and data.get("requestType") == "GetInputPropertiesListPropertyItems":
            lists.append(data["responseData"]["propertyItems"])
    return lists


class ListingObs(FakeObs):
    def __init__(
        self,
        *,
        input_kinds: Iterable[str],
        active: Iterable[str] = (),
        unconfigured: Iterable[str] = ("GetReplayBufferStatus", "GetVirtualCamStatus"),
        changed_event: str = "before",
        event_delay_s: float = 0.05,
        **kwargs: Any,
    ) -> None:
        super().__init__(input_kinds=input_kinds, **kwargs)
        self.active = set(active)
        self.unconfigured = set(unconfigured)
        self.changed_event = changed_event
        self.event_delay_s = event_delay_s
        self.listed_in: list[str] = []
        """The current collection at each window-list request."""

    def statuses(self) -> list[str]:
        return [name for name in self.names() if name in STATUS_REQUESTS]

    def _status(self, request: str) -> dict[str, Any]:
        if request in self.active:
            return {"outputActive": True}
        if request in self.unconfigured:
            raise ObsRequestError(request, NOT_AVAILABLE, "not available.")
        return {"outputActive": False}

    def _GetStreamStatus(self) -> dict[str, Any]:
        return self._status("GetStreamStatus")

    def _GetRecordStatus(self) -> dict[str, Any]:
        return self._status("GetRecordStatus")

    def _GetReplayBufferStatus(self) -> dict[str, Any]:
        return self._status("GetReplayBufferStatus")

    def _GetVirtualCamStatus(self) -> dict[str, Any]:
        return self._status("GetVirtualCamStatus")

    def _announce(self, *events: tuple[str, dict[str, Any]]) -> None:
        """``FakeObs``'s hook for the events of a switch it has made: the switch back's per ``changed_event``."""
        changed = dict(events).get(ObsEventName.CURRENT_SCENE_COLLECTION_CHANGED)
        switch_back = changed is not None and changed.get("sceneCollectionName") != OBS_COLLECTION_NAME
        if not switch_back or self.changed_event == "before":
            super()._announce(*events)
        elif self.changed_event == "after":
            timer = threading.Timer(self.event_delay_s, self._announce_now, (events,))
            timer.daemon = True
            timer.start()

    def _GetInputPropertiesListPropertyItems(self, inputName: str, propertyName: str) -> dict[str, Any]:  # noqa: N803
        self.listed_in.append(self.current_collection)
        return super()._GetInputPropertiesListPropertyItems(inputName=inputName, propertyName=propertyName)
