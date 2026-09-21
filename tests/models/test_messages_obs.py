from pathlib import Path

import pytest

from anki_miner_game.models.addons import AddonStatus
from anki_miner_game.models.lines import GameLine
from anki_miner_game.models.messages import (
    OBS_SOURCE_ID,
    AppState,
    Banner,
    BannerCleared,
    BannerLevel,
    BannerRaised,
    CommandKind,
    LineAccepted,
    LineReceived,
    ObsEvent,
    RecordingStarted,
    RecordingStopped,
    SessionEvent,
    SessionFinalised,
    SessionInput,
    SourceStatus,
    SourceStatusChanged,
    StateChanged,
    Tick,
    UserCommand,
)
from anki_miner_game.models.obs import (
    REQUIRED_REQUESTS,
    ObsAuthError,
    ObsConfigError,
    ObsConnectError,
    ObsCredentials,
    ObsError,
    ObsEventName,
    ObsInfo,
    ObsInstall,
    ObsRequestError,
    ObsUnsupportedError,
    OutputState,
    ProvisionResult,
    WindowItem,
    WsConfig,
)


def test_enum_values_are_the_spec_strings():
    assert [s.value for s in AppState] == ["idle", "armed", "recording", "finalising"]
    assert [s.value for s in SourceStatus] == ["disconnected", "connecting", "connected", "receiving"]
    assert [k.value for k in CommandKind] == ["arm", "disarm", "start", "stop", "toggle"]
    assert [lvl.value for lvl in BannerLevel] == ["info", "warning", "error"]
    assert [s.value for s in AddonStatus] == ["missing", "installing", "ready", "broken"]
    assert OutputState.PAUSED == "OBS_WEBSOCKET_OUTPUT_PAUSED"
    assert OutputState.STARTED == "OBS_WEBSOCKET_OUTPUT_STARTED"
    assert ObsEventName.RECORD_STATE_CHANGED == "RecordStateChanged"
    assert OBS_SOURCE_ID == "obs"


def test_gateway_connection_events_cannot_collide_with_obs_events():
    assert ObsEventName.CONNECTED.startswith("_")
    assert ObsEventName.CONNECTION_LOST.startswith("_")


def test_session_input_and_event_unions_cover_every_message():
    line = GameLine(text="a", raw="a", t_mono=1.0, source_id="agent")
    inputs = [
        LineReceived(raw="a", t_mono=1.0, source_id="agent"),
        ObsEvent(name="ExitStarted", data={}, t_mono=2.0),
        UserCommand(kind=CommandKind.ARM, slug="steins-gate"),
        Tick(t_mono=3.0),
    ]
    events = [
        StateChanged(AppState.ARMED),
        LineAccepted(line=line, offset_ms=None),
        RecordingStarted(stem="2026-10-02 18-04-11"),
        RecordingStopped(stem="2026-10-02 18-04-11"),
        SessionFinalised(manifest_path=Path("x.session.json")),
        SourceStatusChanged(source_id=OBS_SOURCE_ID, status=SourceStatus.CONNECTED),
        BannerRaised(Banner(key="no-source", level=BannerLevel.WARNING, text="No text source")),
        BannerCleared(key="no-source"),
    ]
    assert all(isinstance(msg, SessionInput) for msg in inputs)
    assert all(isinstance(ev, SessionEvent) for ev in events)
    assert not isinstance(Tick(1.0), SessionEvent)


def test_line_accepted_defaults_to_a_new_line():
    line = GameLine(text="a", raw="a", t_mono=1.0, source_id="agent")
    assert LineAccepted(line=line, offset_ms=5).replaces_previous is False
    assert UserCommand(kind=CommandKind.STOP).slug is None


def test_required_requests_are_the_26_of_spec_3_3():
    assert len(REQUIRED_REQUESTS) == 26
    assert len(set(REQUIRED_REQUESTS)) == 26
    assert REQUIRED_REQUESTS[0] == "GetVersion"
    assert REQUIRED_REQUESTS[-1] == "GetInputPropertiesListPropertyItems"


def test_missing_requests_keeps_the_required_order():
    info = ObsInfo(
        obs_version="29.1.3",
        websocket_version="5.3.0",
        available_requests=frozenset(REQUIRED_REQUESTS) - {"SetInputMute", "GetVersion"},
    )
    assert info.missing_requests() == ("GetVersion", "SetInputMute")
    full = ObsInfo(obs_version="31.0.2", websocket_version="5.5.4", available_requests=frozenset(REQUIRED_REQUESTS))
    assert full.missing_requests() == ()


def test_passwords_never_appear_in_repr():
    creds = ObsCredentials(host="127.0.0.1", port=4455, password="hunter2")
    ws = WsConfig(server_enabled=True, port=4455, password="hunter2", auth_required=True)
    assert "hunter2" not in repr(creds)
    assert "hunter2" not in repr(ws)
    assert creds.password == "hunter2"


def test_small_records():
    install = ObsInstall(argv=("flatpak", "run", "com.obsproject.Studio"), cwd=None, flatpak=True)
    assert install.flatpak is True
    assert WindowItem(name="Game", value="Game:UnityWndClass:game.exe", enabled=True).value.endswith(".exe")
    assert ProvisionResult(changed=True, needs_restart=False).changed is True


def test_window_item_enabled_is_required_after_value():
    stale = WindowItem("[game.exe]: Game", "Game:UnityWndClass:game.exe", False)
    assert stale.enabled is False
    with pytest.raises(TypeError):
        WindowItem(name="Game", value="Game:UnityWndClass:game.exe")  # type: ignore[call-arg]


def test_obs_error_hierarchy_and_messages():
    assert issubclass(ObsAuthError, ObsConnectError)
    assert issubclass(ObsConfigError, ObsConnectError)
    assert issubclass(ObsConnectError, ObsError)
    err = ObsRequestError("StartRecord", 500, "Output is already active")
    assert (err.request, err.code, err.comment) == ("StartRecord", 500, "Output is already active")
    assert "Output is already active" in str(err)
    unsupported = ObsUnsupportedError("29.1.3", ("SetInputMute",))
    assert unsupported.missing == ("SetInputMute",)
    assert "SetInputMute" in str(unsupported)
    assert "29.1.3" in str(unsupported)
