# T12 OBS gateway + FakeObsServer: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans (one implementer, tasks in
> the order below: Tasks 1-2 build one file and Tasks 3-4 another, step by step, so do not split
> them across agents) with superpowers:test-driven-development inside every task. Steps use
> checkbox (`- [ ]`) syntax.

**Goal:** Give the session actor one obs-websocket v5 connection it can trust (`obs/client.py`,
the `ObsGateway` of spec 11.2) and give every OBS-facing test a real protocol peer
(`tests/fakes/fake_obs_server.py`, spec 18.2) that also replays sessions recorded from a real OBS.

**Architecture:** `ObsClient` wraps one obsws-python `ReqClient` + `EventClient` pair (a "link").
Every blocking library call runs on the gateway's own single-thread executor, awaited from the I/O
loop. Events are stamped with the injected `now()` on obsws-python's event thread and handed raw
to the subscribed handlers under one lock, so handlers see `_Connected`, then that link's events in
order, then `_ConnectionLost`. `connect` opens both clients (a refused password re-reads the
credentials once), checks `GetVersion` (207 retried) against `REQUIRED_REQUESTS` and announces
`_Connected`; `request` waits while `collection_changing`, retries 207 and turns a transport
failure into a lost link, which starts a backoff reconnect loop that re-reads the credentials at
every attempt. `FakeObsServer` is a `websockets` server speaking ops 0/1/2/5/6/7 with SHA-256
auth, answering from a reply table, and replaying JSONL transcripts in the format of
`tools/obs_transcript_recorder.py`.

**Tech Stack:** Python 3.12, obsws-python 1.8.0 (websocket-client 1.9.2 underneath), websockets
17.1 (the fake only), pytest 9 + pytest-asyncio 1.4 (`asyncio_mode = "auto"`), black, ruff, mypy.

**Spec:** `/home/light/Projects/anki_miner_game/.worktrees/t12-obs-gateway/docs/specs/2026-09-20-anki-miner-game-design.md`
sections 3.3, 4.2, 11.1 (readiness, credentials), 11.2, 17 (row "Authentication fails"), 18.2
(`FakeObsServer`); the master plan
`/home/light/Projects/anki_miner_game/.worktrees/t12-obs-gateway/docs/plans/2026-09-21-master-plan.md`
sections 1-4 and card `### T12 OBS gateway + FakeObsServer`; the M0 inputs
`docs/m0/source-findings.md` (summary items 5, 7, 11, 16; sections 5, 8, 11),
`docs/m0/wave-1-amendments.md` item 9, `docs/contracts.md` (rows `ObsGateway.close()`,
`ObsError` hierarchy, `ObsEventName`, `ObsConfigError`, the W1 207 docstrings), and after the M0
gate `docs/m0/obs-behaviour.md` (R2).

## Global Constraints

Verbatim from the master plan; every task below inherits them.

- Standalone app, own repository. **No Anki Miner changes, ever.** Contract = files on disk.
- Windows first-class, Linux best-effort, **macOS unsupported**.
- Python 3.12, PyQt6, PyInstaller. Licence **GPL-3.0-only**.
- Frozen runtime deps: `PyQt6`, `obsws-python`, `websockets`. "Nothing else in the frozen app."
- `models` imports nothing from the app; `session`, `text`, `obs`, `vad`, `feed`, `lifecycle` never
  import `gui`; `gui` reaches the rest only through `interfaces`. `session/cues.py` and
  `vad/assign.py` are pure. Composition lives in `app.py`; no DI container.
- All models are frozen dataclasses; JSON on disk carries `"schema": 1`.
- `SKIP_MS = 300`, `MIN_CUE_MS = 500` and every VAD threshold are module constants, not settings.
  `START_SHIFT_MS = {"hook": 0, "ocr": -1000}`.
- A line's arrival time is `time.monotonic()` read inside the source at frame receipt; nothing
  downstream re-stamps it.
- Cue invariant: `0 <= start < end <= next.start`.
- SRT: UTF-8 without BOM, `\n`, index from 1, `HH:MM:SS,mmm` from integer ms, one text line per cue,
  written to a temporary name then `os.replace`.
- Feed binds `127.0.0.1`. The OBS password is never logged and never stored unless typed as an override.
- The app never reads or writes `~/.config/owocr_config.ini`.
- The bundle contains no onnxruntime, numpy, PyAV or owocr (spec `excludes` + smoke assertion).
- Recoverable failures are banners, never modal dialogs. No `pynput`.
- Tests: pytest-qt offscreen, `qtbot.addWidget` on every top-level widget, isolated
  `ANKI_MINER_GAME_HOME` per test, no network except loopback.
- Ported code keeps a header naming the source file and commit (GSM `479747fe`, faster-whisper,
  owocr 1.26.8, Anki Miner commit).

---

## Working context

- Worktree `/home/light/Projects/anki_miner_game/.worktrees/t12-obs-gateway`, branch
  `feat/t12-obs-gateway`, BASE `052cc99a96bb65274292cdb18d90e0b9cef6700d` (main; T12 has no
  dependency branches). `.venv` is a symlink to `/home/light/Projects/anki_miner_game/.venv`.
  Implementation starts after the M0 gate, which commits the R2 transcripts
  (`tests/fixtures/obs_transcripts/*.jsonl`), `docs/m0/obs-behaviour.md` and the spec amendments
  on `main`: merge it first,
  `git -C /home/light/Projects/anki_miner_game/.worktrees/t12-obs-gateway merge --no-ff main`.
  If `main` also carries T14's `REQUIRED_REQUESTS` extension (31 names), nothing here changes:
  every test derives the list from `anki_miner_game.models.obs.REQUIRED_REQUESTS`.
- Read `/home/light/Projects/anki_miner_game/.worktrees/t12-obs-gateway/CLAUDE.md` first. Never
  bare `python3`, never `uv run`, never `pip install -e`, no installs at all. Every Bash call starts
  with `cd /home/light/Projects/anki_miner_game/.worktrees/t12-obs-gateway &&`.
- Status file: `/home/light/Projects/anki_miner_game/.orchestration/status/t12-obs-gateway.json`
  (keep `base_sha`).
- **Never launch OBS, never touch `~/.var/app/com.obsproject.Studio`, never start a nested
  display.** Every test here runs against `FakeObsServer` on 127.0.0.1 with an OS-assigned port.
- Files you own (create all of them; nothing else changes):
  - `anki_miner_game/obs/client.py`
  - `tests/fakes/fake_obs_server.py`, `tests/fakes/obs_transcript_sample.py`,
    `tests/fakes/test_fake_obs_server.py`
  - `tests/obs/__init__.py` (empty; T13 and T14 add the same empty file, which merges cleanly),
    `tests/obs/conftest.py`, `tests/obs/helpers.py`, `tests/obs/test_client.py`,
    `tests/obs/test_client_transcripts.py`
  - read-only for this task: `models/`, `interfaces/` (contract), `anki_miner_game/obs/__init__.py`
    (stays empty), `tests/fixtures/obs_transcripts/*` (R2 owns them).
- Single-file test runs below pass `-n0 -p no:cacheprovider` to skip xdist start-up; Task 6 runs
  the real gate configuration. Before each commit run
  `./.venv/bin/black <touched files> && ./.venv/bin/ruff check <touched files>` and fix what they
  report (black may re-wrap long lines of the code below; that is expected).
- Context7 (implementer prompt rule 3): before Task 1 query `/python-websockets/websockets` for the
  17.x asyncio server (`serve(..., process_request=, close_timeout=)`,
  `ServerConnection.respond`, `.transport.abort()`), and before Task 3 `/aatikturk/obsws-python`.
  The installed sources win on any disagreement: obsws-python 1.8.0 is
  `.venv/lib/python3.12/site-packages/obsws_python/`, and S1 read it (`source-findings.md` s11).
- **The code in this plan was run while planning.** A prototype of every file, at the paths above,
  passed black, ruff, mypy and the full suite (1135 passed) in this worktree; the Task 1 and Task 3
  intermediate states passed their own tests; the Task 4 tests failed on the Task 3 code as
  listed in Task 4; and the replay test passed on all 24 draft transcripts in R2's worktree
  (read-only) and on 29 raw R2 runs. The prototype was deleted before this plan was committed; you
  write the files again, test first.

## Decisions made while planning

Nobody answers questions during this run; these were settled from the card, the spec, the M0
findings and the installed library sources.

1. **The constructor takes a credentials callable, not an `ObsDiscovery`.**
   `ObsClient(credentials: Callable[[], ObsCredentials], *, now=time.monotonic, sleep=asyncio.sleep)`.
   `app.py` (T16) passes `lambda: discovery.credentials(<current AppConfig>)`, so "credentials from
   `ObsDiscovery.credentials` at every connect" holds and a typed password override is seen at the
   next attempt, while the gateway stays testable with a plain function. The callable runs on the
   gateway's worker thread (it reads OBS's `config.json`). Its `ObsConfigError` passes through
   unchanged, as the `ObsDiscovery.credentials` docstring promises.
2. **The banner is the actor's; the gateway raises.** "On auth failure re-read once, then a banner"
   (card, spec 17): an attempt whose Identify OBS refuses calls `credentials()` once more and
   retries; a second refusal raises `ObsAuthError`. A refusal is recognised as "Hello carried an
   `authentication` challenge and Identify failed" (OBS closes with 4009; obsws-python surfaces it
   as an `OBSSDKError`, and raises the same type when no password was given), so a missing
   password counts too. While the background reconnect loop fails on the password, `request()`
   raises `ObsAuthError` instead of `ObsConnectError`, so T15 can raise the password banner from
   either path. The flag clears at the next successful connect. No contract change:
   `ObsAuthError` is an `ObsConnectError`.
3. **Reconnect starts only after a connection that succeeded is lost.** A failed first
   `connect()` raises and starts nothing: the caller (arming) decides to launch OBS and wait. After
   a loss the gateway reconnects (`BACKOFF_S` = 1, 2, 5, 10, 10 ... s, the text sources'
   schedule, kept as a constant of its own: importing one tuple from `text/sources/` would couple
   two packages that share nothing else) until it succeeds or `close()` is called; `connect()`
   during the backoff tries at once (one `asyncio.Lock` serialises attempts), and the loop then
   sees the connection and stops.
   `close()` sends no `_ConnectionLost` and leaves the gateway reusable: a later `connect()` opens
   a new connection.
4. **A lost link** is: obsws-python's event thread ending (the socket closed), or a request failing
   below the protocol (closed socket, unreadable or mismatched reply) or timing out
   (`REQUEST_TIMEOUT_S`, the socket timeout; obsws-python cannot match a late reply, so a client
   is never reused after one, S1 summary 16). Teardown aborts both sockets (which wakes a request
   blocked in `recv`), closes them without a close handshake and joins the event thread.
5. **Raw event data.** obsws-python converts `eventData` to snake-case dataclasses and dispatches
   by function name; `ObsEvent.data` must be `eventData` as OBS sent it (camelCase `outputState`,
   `outputPath`, `newOutputPath`). `_EventClient` subclasses `obsws_python.EventClient`, swaps in a
   raw `_RawCallback` in `subscribe()` (before the reader thread starts) and wraps `trigger()` in
   `try/finally` to report the thread's end. Every event of the subscribed categories is
   forwarded, not only `ObsEventName` members; the actor ignores the rest. A handler that raises is
   logged and skipped; the others still run and the link stays up.
6. **Subscriptions:** the `EventClient` identifies with General | Config | Outputs (= 67):
   `ExitStarted`, profile and collection switches, record events. The `ReqClient` identifies with
   0, so OBS never sends it an event frame that obsws-python would take for a response.
7. **`_Connected` comes first.** `connect` opens both clients, then checks `GetVersion`, then
   announces `_Connected`. Events that reach the new link before that (the R2 recordings have
   `CurrentProfileChanged` right after Identify) are held on the link, with their arrival stamps,
   and delivered right after `_Connected` (spec 6.3: reconcile runs "before any event is
   trusted"). A failed connect drops them.
8. **`collection_changing`** is set by a delivered `CurrentSceneCollectionChanging` and cleared by
   `CurrentSceneCollectionChanged`, by a lost connection and by `close()`. `request` polls it
   (`COLLECTION_POLL_S`) until it clears or `NOT_READY_TIMEOUT_S` passes, then sends anyway: a lost
   `Changed` event must not hold requests forever, and OBS still answers 207 while really changing.
   The wait and the 207 retries share one deadline per request.
9. **207 `NotReady`** is retried every `NOT_READY_RETRY_S` until `NOT_READY_TIMEOUT_S` (30 s, the
   budget of `ObsDiscovery.wait_ready`), then raised as
   `ObsRequestError(name, 207, <OBS's comment>)`, in `connect` (`GetVersion`) and in `request`.
   Any other failure status raises at once, no retry.
10. **Requests go through `ReqClient.base_client.req`, not `ReqClient.send`.** `send` raises
    `OBSSDKRequestError`, which keeps `code` but not `comment` (`obsws_python/error.py`), and logs
    every failure with a traceback; `ObsRequestError` needs the comment. `base_client.req` returns
    the reply's `d`; the gateway checks `requestType` and `requestStatus` itself.
11. **Logging.** The `obsws_python` logger is set to `CRITICAL` in `ObsClient.__init__`, before any
    client exists: the library logs the password at INFO and every refused connect with a
    traceback at ERROR, which the gateway reports itself in one line. T13's `discovery.py` sets the
    same logger to WARNING at import; the two agree (the gateway's is stricter). No gateway message
    contains the password; `ObsCredentials.password` is `repr=False` already.
12. **Provisional constants.** `NOT_READY_TIMEOUT_S`, `NOT_READY_RETRY_S` and `REQUEST_TIMEOUT_S`
    depend on what R2 measures (OBS load time, the longest collection switch, the slowest blocking
    request); they sit together at the top of `client.py` under a "Provisional until R2" comment.
    Task 5 settles them from `docs/m0/obs-behaviour.md`. `BACKOFF_S`, `COLLECTION_POLL_S`,
    `JOIN_TIMEOUT_S`, `EVENT_SUBSCRIPTIONS` and `LIBRARY_LOG_LEVEL` do not depend on R2.
13. **FakeObsServer replay model.** R2 drives OBS through `tools/m0/obs_scenarios.py`'s `ObsLink`
    (a `ReqClient`, Identify `eventSubscriptions` 0, then an `EventClient`, `Subs.LOW_VOLUME` =
    2047) behind the recorder proxy. The loader turns a recording into steps:
    - a connection's role comes from its Identify (0: requests, anything else: events);
    - a *generation* is an event connection plus the request connection opened most recently
      before it and still open; every other connection (the second polling client of
      `switch_not_ready`, the extra `SetCurrentProfile` clients of the restart-prompt scenarios, a
      refused handshake) belongs to another client and its frames are dropped; generations must
      not overlap in time (the loader raises `ValueError` naming the file otherwise);
    - `GetVersion` exchanges are dropped and the first successful recorded answer becomes the
      fake's live `GetVersion` answer, because the gateway sends its own at every connect;
    - Hello/Identify/Identified are dropped: the fake runs the handshake live, without
      authentication (the recorded authentication is redacted).

    The fake plays the steps in recorded order: an `open` binds the next identified client of that
    role (whatever order the gateway connects in); a `request` waits for the bound client to send
    it and records any mismatch in `unscripted`; a `response` goes out with the client's request
    id; an `event` goes out when the client's subscriptions cover its recorded `eventIntent` (so
    the LOW_VOLUME recording replays faithfully to the gateway's narrower mask); an OBS-side
    `close` closes with the recorded code (abort for 1006) and, in the last generation, makes the
    fake refuse further handshakes as a gone OBS does; a client-side `close` with a later
    generation cuts the connection, since the recorded client reconnected there.
14. **No contract change request.** `ObsClient` matches `interfaces/obs.py:ObsGateway` as written
    (`tests/test_contracts.py::_assert_conforms` pins it); `ObsEvent(name, data, t_mono)` with `{}`
    data for the gateway's own events.
15. **Known limit, reported, not fixed here:** websocket-client 1.9.2 (under obsws-python) routes a
    `ws://127.0.0.1` connection through `http_proxy` when that variable is set and `no_proxy` does
    not list the host (`websocket/_url.py:108-200`), and obsws-python passes no proxy options. The
    tests clear `http_proxy`/`HTTP_PROXY` (autouse fixture in `tests/obs/conftest.py`); the app is
    flagged for H5 and the user guide, not worked around.

## File structure

| File | Responsibility |
|---|---|
| `anki_miner_game/obs/client.py` | `ObsClient` (the `ObsGateway`), its constants, `_Link`, `_ReqClient`, `_EventClient`, `_RawCallback`, `_identify_failed`, `_blocking_send` |
| `tests/fakes/fake_obs_server.py` | `FakeObsServer`, `Reply`, `RecordedRequest`, `version_data`, `auth_string`, `EVENT_CATEGORY`, `Step`/`Exchange`/`Transcript`, `load_transcript` |
| `tests/fakes/obs_transcript_sample.py` | One hand-written transcript in the recorder's format (every record labelled synthetic), `write_transcript`, `write_sample_transcript`, `SAMPLE_VERSION` |
| `tests/fakes/test_fake_obs_server.py` | The fake against a plain `websockets` client: protocol, auth, reply table, stall, event filter, refuse, drop, loader, replay binding |
| `tests/obs/helpers.py` | `PASSWORD`, `CONNECTED`, `LOST`, `Credentials`, `FakeClock`, `ThreadClock`, `Events`, `wait_until` |
| `tests/obs/conftest.py` | `_no_proxy` (autouse), `obs_server` (a started `FakeObsServer(password=PASSWORD)`), `make_gateway` (gateways closed at teardown) |
| `tests/obs/test_client.py` | Gateway behaviour on the live fake (Tasks 3-4) |
| `tests/obs/test_client_transcripts.py` | Gateway replay of the sample transcript and of every R2 transcript (Task 5) |

## Interfaces produced

Later tasks rely on exactly these names: T15 (the actor; its tests use a fake gateway, but T15
reads the error behaviour below), T16 (composition), T14 and T25 (`FakeObsServer`,
`load_transcript`).

```python
# anki_miner_game/obs/client.py
NOT_READY_TIMEOUT_S: Final = 30.0      # provisional until R2
NOT_READY_RETRY_S: Final = 0.25        # provisional until R2
REQUEST_TIMEOUT_S: Final = 20.0        # provisional until R2
NOT_READY: Final = 207
BACKOFF_S: Final = (1.0, 2.0, 5.0, 10.0)
COLLECTION_POLL_S: Final = 0.05
JOIN_TIMEOUT_S: Final = 2.0
EVENT_SUBSCRIPTIONS: Final = 67        # obsws Subs.GENERAL | CONFIG | OUTPUTS
LIBRARY_LOG_LEVEL: Final = logging.CRITICAL

class ObsClient:                       # satisfies interfaces.obs.ObsGateway
    def __init__(self, credentials: Callable[[], ObsCredentials], *,
                 now: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None: ...
    async def connect(self) -> ObsInfo: ...          # ObsConnectError | ObsAuthError | ObsConfigError
                                                     # | ObsUnsupportedError | ObsRequestError(207)
    async def request(self, name: str, **fields: Any) -> dict[str, Any]: ...
                                                     # ObsRequestError | ObsConnectError
                                                     # | ObsAuthError (reconnect refused the password)
    def subscribe(self, handler: Callable[[ObsEvent], None]) -> None: ...
    @property
    def collection_changing(self) -> bool: ...
    async def close(self) -> None: ...               # no _ConnectionLost; connect() works again after

# tests/fakes/fake_obs_server.py
OP_HELLO, OP_IDENTIFY, OP_IDENTIFIED, OP_EVENT, OP_REQUEST, OP_RESPONSE = 0, 1, 2, 5, 6, 7
RPC_VERSION = 1; SUCCESS = 100; NOT_READY = 207; AUTH_FAILED = 4009; NOT_IDENTIFIED = 4007
NOT_READY_COMMENT = "OBS is not ready to perform the request."
SUBSCRIBE_ALL = 4095; CLOSE_TIMEOUT_S = 0.5
EVENT_CATEGORY: dict[str, int]
Role = Literal["requests", "events"]
def auth_string(password: str, salt: str, challenge: str) -> str: ...
def version_data(*, obs_version: str = "32.2.2", websocket_version: str = "5.7.4",
                 available_requests: Iterable[str] = REQUIRED_REQUESTS) -> dict[str, Any]: ...
@dataclass(frozen=True) class Reply: data: Mapping[str, Any] | None = None; code: int = 100; comment: str | None = None
NOT_READY_REPLY = Reply(code=207, comment=NOT_READY_COMMENT)
@dataclass(frozen=True) class RecordedRequest: client: int; request_type: str; request_data: Mapping[str, Any]
@dataclass(frozen=True) class Step: conn; kind: Literal["open", "request", "response", "event", "close"]; d; by; code; reason
@dataclass(frozen=True) class Exchange: generation: int; request_type: str; request_data: Mapping[str, Any]; response: Mapping[str, Any] | None
@dataclass(frozen=True) class Transcript:
    name: str; roles: Mapping[int, Role]; generations: tuple[tuple[int, int], ...]
    steps: tuple[Step, ...]; version: Mapping[str, Any] | None
    def generation_of(self, conn: int) -> int: ...
    def is_last_generation(self, conn: int) -> bool: ...
    def close_of(self, conn: int) -> Step | None: ...
    def exchanges(self) -> list[Exchange]: ...
    def expected_events(self, subscriptions: int, connected: str, lost: str) -> list[tuple[str, dict[str, Any]]]: ...
def load_transcript(path: Path) -> Transcript: ...
class FakeObsServer:
    def __init__(self, *, password: str | None = None, version: Mapping[str, Any] | None = None,
                 transcript: Transcript | None = None) -> None: ...
    password: str | None            # checked at each Identify; change it to refuse later logins
    version: dict[str, Any]         # the live GetVersion answer
    refuse_connections: bool        # True: handshakes get HTTP 503
    handshakes: int; identifies: list[dict[str, Any]]; auth_failures: int
    requests: list[RecordedRequest]; errors: list[str]; unscripted: list[str]
    replay_finished: asyncio.Event; replay_position: str
    async def start(self) -> None; async def stop(self) -> None   # also `async with`
    @property port -> int; @property client_count -> int
    async def wait_for_client_count(self, n: int, timeout: float = 5.0) -> None
    def set_reply(self, request_type: str, *replies: Reply) -> None     # in turn, the last repeats
    def stall(self, request_type: str) -> None
    def request_types(self) -> list[str]
    async def emit(self, event_type: str, data: Mapping[str, Any] | None = None, *, intent: int | None = None) -> None
    async def drop_clients(self, code: int | None = None, reason: str = "") -> None
```

## Which tests need which transcript

| Test | Needs |
|---|---|
| everything in `tests/fakes/test_fake_obs_server.py` | nothing recorded; the loader and replay tests use the hand-written sample (`tests/fakes/obs_transcript_sample.py`) |
| everything in `tests/obs/test_client.py` | nothing recorded (live fake) |
| `test_client_transcripts.py::test_the_gateway_replays_the_sample_transcript` | nothing recorded |
| `test_client_transcripts.py::test_the_recorded_transcripts_are_present` | at least one file in `tests/fixtures/obs_transcripts/` (fails until the M0 gate has merged R2) |
| `test_client_transcripts.py::test_the_gateway_replays_a_recorded_obs_session[<stem>]` | one run per `tests/fixtures/obs_transcripts/*.jsonl`, whatever R2 committed; the set itself is pinned by R2's own `tests/test_obs_transcript_fixtures.py` |

What each R2 transcript exercises in the gateway replay. The names are R2's draft set on
2026-09-21 (24 files in its worktree), provisional until the M0 gate; the test globs, so a renamed
or added file needs no change here. G = generations, X = exchanges the test sends.

| Transcript (spec 18.2 scenario) | G | X | What the replay checks beyond request/answer pairs |
|---|---|---|---|
| `normal` (normal session) | 1 | 26 | STARTING/STARTED/STOPPING/STOPPED delivered raw, in order |
| `pause_resume` (pause and resume) | 1 | 12 | PAUSED/RESUMED edges |
| `pause_noop` | 1 | 6 | `ResumeRecord` 503 raised as `ObsRequestError` without a comment |
| `missed_pause` (missed pause event) | 2 | 7 | cut, `_ConnectionLost`, reconnect, RESUMED only on the new link |
| `reconnect` (reconnect mid-session) | 2 | 7 | cut, `_ConnectionLost`, `_Connected` again, requests on generation 2 |
| `obs_exit` (OBS exit) | 1 | 3 | `ExitStarted` without data, close 1001, `_ConnectionLost`, reconnects refused |
| `obs_sigterm` | 2 | 3 | a second generation with no requests |
| `obs_killed` | 1 | 3 | abort (1006) on both links, `_ConnectionLost`, reconnects refused |
| `arm_disarm`, `switch_recording`, `switch_streaming`, `switch_replay_buffer` (profile and collection switch) | 1 | 21-26 | Changing/Changed around an in-flight `SetCurrentSceneCollection`, `collection_changing` never blocks the next request; 604 failures with comments |
| `switch_refused` (refused switch) | 1 | 9 | 600/601 failures, some without comments |
| `switch_not_ready` | 1 | 4 | the second polling client (and its 207s) dropped as another client's |
| `switch_restart_prompt`, `switch_restart_prompt_matched` | 1 | 4 / 0 | the unanswered `SetCurrentProfile` clients dropped; their profile events still delivered |
| `provision`, `settings_apply`, `window_retitle`, `split_off_runtime`, `start_failed_missing_dir`, `start_failed_unwritable`, `synthetic-arm-virtualcam-active` | 1 | 4-38 | Inputs/Scenes/SceneItems events filtered out by the 67 mask; 604/702 failures |
| `split` | 1 | 7 | `RecordFileChanged` delivered raw (`newOutputPath`) |

If the M0 gate commits a transcript whose shape the loader refuses (overlapping generations) or
whose replay gets stuck, the failure message names the file and the step
(`FakeObsServer.replay_position`). Extend the loader rule for that shape in
`tests/fakes/fake_obs_server.py` with a sample test first; if the recording shows the recorded
client sending a request between a `CurrentSceneCollectionChanging` it did not cause and the
matching `Changed` (the gateway defers such a request, so the replay would wait for a request the
gateway holds back), the rule is: the loader moves that request after the `Changed` and drops its
207 answers. Neither case occurs in the 24 draft files, so neither rule is written now.

---

## Task 1: FakeObsServer, live mode

**Files:**
- Create: `tests/fakes/test_fake_obs_server.py`
- Create: `tests/fakes/fake_obs_server.py`

The fake speaks obs-websocket 5.7.4 as the source does (`obs-websocket@1ef34bf4
src/websocketserver/WebSocketServer_Protocol.cpp:90-125` Identify and 4009, `:212-232` request
handling, the 207 comment and "comment only when non-empty", `:356-367` events with
`eventIntent`, `eventData` only when an object; `EventSubscription::All` = 4095). An idle
obsws-python `ReqClient` never reads, so it never answers a close frame: the server's
`close_timeout` is 0.5 s instead of websockets' 10 s, or every drop would stall that long.

- [ ] **Step 1: Write the failing tests**

`tests/fakes/test_fake_obs_server.py`:

```python
"""FakeObsServer against a plain websockets client: the protocol, the live controls, the loader, the replay."""

import asyncio
import json
from typing import Any

import pytest
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from anki_miner_game.models.obs import REQUIRED_REQUESTS
from tests.fakes.fake_obs_server import (
    AUTH_FAILED,
    NOT_IDENTIFIED,
    FakeObsServer,
    RecordedRequest,
    Reply,
    auth_string,
    version_data,
)

PASSWORD = "fake-server-pw"


async def open_client(server: FakeObsServer, *, subs: int = 0, password: str | None = PASSWORD) -> ClientConnection:
    """Connect and identify as obsws-python does; returns the identified connection."""
    ws = await connect(f"ws://127.0.0.1:{server.port}", proxy=None, ping_interval=None)
    hello = json.loads(await ws.recv())
    assert hello["op"] == 0
    d: dict[str, Any] = {"rpcVersion": 1, "eventSubscriptions": subs}
    if "authentication" in hello["d"] and password is not None:
        auth = hello["d"]["authentication"]
        d["authentication"] = auth_string(password, auth["salt"], auth["challenge"])
    await ws.send(json.dumps({"op": 1, "d": d}))
    return ws


async def identified(server: FakeObsServer, **kwargs: Any) -> ClientConnection:
    ws = await open_client(server, **kwargs)
    assert json.loads(await ws.recv()) == {"op": 2, "d": {"negotiatedRpcVersion": 1}}
    return ws


async def ask(ws: ClientConnection, request_type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    request: dict[str, Any] = {"requestType": request_type, "requestId": "r1"}
    if data:
        request["requestData"] = data
    await ws.send(json.dumps({"op": 6, "d": request}))
    frame = json.loads(await ws.recv())
    assert frame["op"] == 7 and frame["d"]["requestId"] == "r1"
    return frame["d"]


@pytest.fixture
async def server():
    async with FakeObsServer(password=PASSWORD) as fake:
        yield fake


# --- protocol ------------------------------------------------------------------------------------


async def test_hello_carries_a_challenge_and_the_right_password_identifies(server):
    ws = await identified(server)

    assert server.client_count == 1
    assert server.auth_failures == 0
    assert [identify["eventSubscriptions"] for identify in server.identifies] == [0]
    await ws.close()


async def test_a_wrong_password_is_refused_with_4009(server):
    ws = await open_client(server, password="wrong")

    with pytest.raises(ConnectionClosed) as raised:
        await ws.recv()

    assert raised.value.rcvd is not None and raised.value.rcvd.code == AUTH_FAILED
    assert server.auth_failures == 1
    assert server.client_count == 0


async def test_without_a_password_hello_asks_for_no_authentication():
    async with FakeObsServer() as server:
        ws = await connect(f"ws://127.0.0.1:{server.port}", proxy=None)
        hello = json.loads(await ws.recv())
        assert "authentication" not in hello["d"]
        assert (hello["d"]["obsStudioVersion"], hello["d"]["rpcVersion"]) == ("32.2.2", 1)
        await ws.close()


async def test_a_request_before_identify_closes_with_4007(server):
    ws = await connect(f"ws://127.0.0.1:{server.port}", proxy=None)
    await ws.recv()
    await ws.send(json.dumps({"op": 6, "d": {"requestType": "GetVersion", "requestId": "x"}}))

    with pytest.raises(ConnectionClosed) as raised:
        await ws.recv()

    assert raised.value.rcvd is not None and raised.value.rcvd.code == NOT_IDENTIFIED
    assert server.errors


# --- live replies ---------------------------------------------------------------------------------


async def test_requests_are_recorded_and_answered_from_the_reply_table(server):
    server.set_reply("GetRecordStatus", Reply({"outputActive": True}), Reply(code=604, comment="nope"), Reply(code=601))
    ws = await identified(server)

    version = await ask(ws, "GetVersion")
    first = await ask(ws, "GetRecordStatus")
    second = await ask(ws, "GetRecordStatus")
    third = await ask(ws, "GetRecordStatus")
    fourth = await ask(ws, "GetRecordStatus")
    other = await ask(ws, "SetCurrentProfile", {"profileName": "Anki Miner Game"})

    assert version["responseData"] == version_data() and version["responseData"]["availableRequests"] == list(
        REQUIRED_REQUESTS
    )
    assert (first["requestStatus"], first["responseData"]) == ({"result": True, "code": 100}, {"outputActive": True})
    assert second["requestStatus"] == {"result": False, "code": 604, "comment": "nope"}
    assert third["requestStatus"] == fourth["requestStatus"] == {"result": False, "code": 601}
    assert "responseData" not in third
    assert other == {
        "requestType": "SetCurrentProfile",
        "requestId": "r1",
        "requestStatus": {"result": True, "code": 100},
    }
    assert server.requests[-1] == RecordedRequest(1, "SetCurrentProfile", {"profileName": "Anki Miner Game"})
    await ws.close()


async def test_a_stalled_request_is_never_answered(server):
    server.stall("SetCurrentProfile")
    ws = await identified(server)
    await ws.send(json.dumps({"op": 6, "d": {"requestType": "SetCurrentProfile", "requestId": "s"}}))

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.2):
            await ws.recv()

    assert server.request_types() == ["SetCurrentProfile"]
    await ws.close()


async def test_events_reach_only_clients_subscribed_to_their_intent(server):
    requests = await identified(server, subs=0)
    outputs = await identified(server, subs=64)
    config = await identified(server, subs=2)

    await server.emit("RecordStateChanged", {"outputState": "OBS_WEBSOCKET_OUTPUT_STARTED"})
    await server.emit("ExitStarted")
    await server.emit("VendorEvent", {"x": 1}, intent=2)

    assert json.loads(await outputs.recv())["d"] == {
        "eventType": "RecordStateChanged",
        "eventIntent": 64,
        "eventData": {"outputState": "OBS_WEBSOCKET_OUTPUT_STARTED"},
    }
    assert json.loads(await config.recv())["d"] == {"eventType": "VendorEvent", "eventIntent": 2, "eventData": {"x": 1}}
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.1):
            await requests.recv()
    for ws in (requests, outputs, config):
        await ws.close()


async def test_refused_connections_get_http_503(server):
    server.refuse_connections = True

    with pytest.raises(InvalidStatus) as raised:
        await connect(f"ws://127.0.0.1:{server.port}", proxy=None)

    assert raised.value.response.status_code == 503
    assert server.handshakes == 1


@pytest.mark.parametrize(("code", "reason"), [(None, ""), (1001, "Server stopping.")])
async def test_drop_clients_closes_every_connection(server, code, reason):
    ws = await identified(server)

    await server.drop_clients(code, reason)

    with pytest.raises(ConnectionClosed) as raised:
        await ws.recv()
    received = raised.value.rcvd
    assert (None if received is None else (received.code, received.reason)) == (
        None if code is None else (code, reason)
    )
    assert server.client_count == 0
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd /home/light/Projects/anki_miner_game/.worktrees/t12-obs-gateway && ./.venv/bin/pytest tests/fakes/test_fake_obs_server.py -n0 -p no:cacheprovider -q`
Expected: collection error, `ModuleNotFoundError: No module named 'tests.fakes.fake_obs_server'`.

- [ ] **Step 3: Write the fake**

`tests/fakes/fake_obs_server.py` (the module docstring already describes the replay mode Task 2
adds):

```python
"""FakeObsServer: an obs-websocket v5 server for tests (spec 18.2), plus the R2 transcript loader.

Speaks the part of the protocol the app uses, as obs-websocket 5.7.4 does (``obs-websocket@1ef34bf4
src/websocketserver/WebSocketServer_Protocol.cpp``): Hello (op 0) with an authentication challenge
when ``password`` is set, Identify (op 1) checked with obs-websocket's SHA-256 scheme and refused
with close code 4009, Identified (op 2), Request (op 6) and RequestResponse (op 7), Event (op 5) sent
only to clients whose ``eventSubscriptions`` cover the event's intent. It binds 127.0.0.1 on an
OS-assigned port.

Two modes:

- Live (no ``transcript``): requests are answered from a reply table (``set_reply``), ``GetVersion``
  from ``version``; ``emit`` sends events; ``stall``, ``refuse_connections`` and ``drop_clients``
  make OBS hang, stay closed or vanish.
- Replay (``transcript=load_transcript(path)``): plays a session recorded from a real OBS by
  ``tools/obs_transcript_recorder.py`` (M0 spike R2, ``tests/fixtures/obs_transcripts/*.jsonl``).
  See ``load_transcript`` for how a recording becomes steps and ``FakeObsServer._replay`` for how
  they are played.
"""

import asyncio
import base64
import contextlib
import hashlib
import itertools
import json
import secrets
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from http import HTTPStatus
from types import TracebackType
from typing import Any, Self

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from anki_miner_game.models.obs import REQUIRED_REQUESTS

OP_HELLO, OP_IDENTIFY, OP_IDENTIFIED, OP_EVENT, OP_REQUEST, OP_RESPONSE = 0, 1, 2, 5, 6, 7
RPC_VERSION = 1
SUCCESS = 100
NOT_READY = 207
NOT_READY_COMMENT = "OBS is not ready to perform the request."
AUTH_FAILED = 4009
NOT_IDENTIFIED = 4007
SUBSCRIBE_ALL = 4095
"""obs-websocket 5.7.4 ``EventSubscription::All`` (General through Canvases)."""

EVENT_CATEGORY: dict[str, int] = {
    "ExitStarted": 1,
    "CurrentSceneCollectionChanging": 2,
    "CurrentSceneCollectionChanged": 2,
    "SceneCollectionListChanged": 2,
    "CurrentProfileChanging": 2,
    "CurrentProfileChanged": 2,
    "ProfileListChanged": 2,
    "SceneCreated": 4,
    "InputCreated": 8,
    "StreamStateChanged": 64,
    "RecordStateChanged": 64,
    "RecordFileChanged": 64,
    "ReplayBufferStateChanged": 64,
    "VirtualcamStateChanged": 64,
}
"""``eventIntent`` of the events tests emit (obs-websocket ``EventSubscription``); replayed events carry their own."""

CLOSE_TIMEOUT_S = 0.5
"""How long a close waits for the client's close frame: an idle obsws-python ``ReqClient`` never reads it."""

_UNSENDABLE_CLOSE = (None, 1005, 1006, 1015)


def auth_string(password: str, salt: str, challenge: str) -> str:
    """The Identify ``authentication`` value obs-websocket expects (``Utils::Crypto``)."""
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest())
    return base64.b64encode(hashlib.sha256(secret + challenge.encode()).digest()).decode()


def version_data(
    *,
    obs_version: str = "32.2.2",
    websocket_version: str = "5.7.4",
    available_requests: Iterable[str] = REQUIRED_REQUESTS,
) -> dict[str, Any]:
    """A ``GetVersion`` ``responseData``; by default an OBS that has every request the app needs."""
    return {
        "obsVersion": obs_version,
        "obsWebSocketVersion": websocket_version,
        "rpcVersion": RPC_VERSION,
        "availableRequests": list(available_requests),
        "platform": "fake",
    }


@dataclass(frozen=True)
class Reply:
    data: Mapping[str, Any] | None = None
    """``responseData``; ``None`` sends none."""
    code: int = SUCCESS
    comment: str | None = None


NOT_READY_REPLY = Reply(code=NOT_READY, comment=NOT_READY_COMMENT)


@dataclass(frozen=True)
class RecordedRequest:
    client: int
    """1-based order in which the client connected."""
    request_type: str
    request_data: Mapping[str, Any]


# --- the server ---------------------------------------------------------------------------------


@dataclass(eq=False)
class _Client:
    conn: ServerConnection
    index: int
    salt: str
    challenge: str
    identified: bool = False
    subs: int = 0


class FakeObsServer:
    def __init__(
        self,
        *,
        password: str | None = None,
        version: Mapping[str, Any] | None = None,
    ) -> None:
        self.password = password
        """Checked at every Identify; ``None`` = authentication off. Change it to refuse later logins."""
        self.version: dict[str, Any] = dict(version) if version is not None else version_data()
        self.refuse_connections = False
        """``True``: every handshake gets HTTP 503, as a closed OBS refuses it."""
        self.handshakes = 0
        """Handshake attempts, refused ones included."""
        self.identifies: list[dict[str, Any]] = []
        """Every Identify ``d`` received, in order."""
        self.auth_failures = 0
        self.requests: list[RecordedRequest] = []
        """Every request received, ``GetVersion`` included, in order."""
        self.errors: list[str] = []
        """Protocol violations by a client."""
        self._replies: dict[str, list[Reply]] = {}
        self._stalled: set[str] = set()
        self._ids = itertools.count(1)
        self._clients: list[_Client] = []
        self._changed = asyncio.Condition()
        self._server: Server | None = None

    # lifecycle

    async def start(self) -> None:
        self._server = await serve(
            self._handle,
            "127.0.0.1",
            0,
            process_request=self._process_request,
            ping_interval=None,
            close_timeout=CLOSE_TIMEOUT_S,
        )

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.stop()

    @property
    def port(self) -> int:
        assert self._server is not None, "start() first"
        return int(self._server.sockets[0].getsockname()[1])

    @property
    def client_count(self) -> int:
        """Identified clients still connected."""
        return sum(1 for client in self._clients if client.identified)

    async def wait_for_client_count(self, n: int, timeout: float = 5.0) -> None:
        async with asyncio.timeout(timeout), self._changed:
            await self._changed.wait_for(lambda: self.client_count == n)

    # live behaviour

    def set_reply(self, request_type: str, *replies: Reply) -> None:
        """Answer the next requests of ``request_type`` with ``replies`` in turn; the last one repeats."""
        self._replies[request_type] = list(replies)

    def stall(self, request_type: str) -> None:
        """Never answer ``request_type`` (a blocking request held up by a modal dialog)."""
        self._stalled.add(request_type)

    def request_types(self) -> list[str]:
        return [request.request_type for request in self.requests]

    async def emit(self, event_type: str, data: Mapping[str, Any] | None = None, *, intent: int | None = None) -> None:
        """Send an event to every identified client subscribed to its intent (``EVENT_CATEGORY`` by default)."""
        d: dict[str, Any] = {
            "eventType": event_type,
            "eventIntent": EVENT_CATEGORY[event_type] if intent is None else intent,
        }
        if data is not None:
            d["eventData"] = dict(data)
        for client in list(self._clients):
            if client.identified and client.subs & d["eventIntent"]:
                await self._send(client, OP_EVENT, d)

    async def drop_clients(self, code: int | None = None, reason: str = "") -> None:
        """Close every connection: with ``code`` as OBS closes them, or abruptly (``None``) as a crash does."""
        dropped = list(self._clients)
        await asyncio.gather(*(self._close(client, code, reason) for client in dropped))
        async with asyncio.timeout(5.0), self._changed:
            await self._changed.wait_for(lambda: not any(client in self._clients for client in dropped))

    # protocol

    def _process_request(self, connection: ServerConnection, request: Request) -> Response | None:
        self.handshakes += 1
        if self.refuse_connections:
            return connection.respond(HTTPStatus.SERVICE_UNAVAILABLE, "OBS is not running\n")
        return None

    async def _handle(self, conn: ServerConnection) -> None:
        client = _Client(conn, next(self._ids), secrets.token_urlsafe(16), secrets.token_urlsafe(16))
        async with self._changed:
            self._clients.append(client)
            self._changed.notify_all()
        try:
            hello: dict[str, Any] = {
                "obsStudioVersion": self.version.get("obsVersion", ""),
                "obsWebSocketVersion": self.version.get("obsWebSocketVersion", ""),
                "rpcVersion": RPC_VERSION,
            }
            if self.password is not None:
                hello["authentication"] = {"challenge": client.challenge, "salt": client.salt}
            await self._send(client, OP_HELLO, hello)
            async for message in conn:
                await self._on_message(client, message)
        except ConnectionClosed:
            pass
        finally:
            async with self._changed:
                self._clients.remove(client)
                self._changed.notify_all()

    async def _on_message(self, client: _Client, message: str | bytes) -> None:
        try:
            frame = json.loads(message)
            op, d = frame["op"], frame["d"]
        except (ValueError, KeyError, TypeError):
            self.errors.append(f"client {client.index}: unreadable frame {message!r}")
            return
        if op == OP_IDENTIFY:
            await self._identify(client, d)
        elif not client.identified:
            self.errors.append(f"client {client.index}: op {op} before Identify")
            await client.conn.close(NOT_IDENTIFIED, "You must identify before sending other messages.")
        elif op == OP_REQUEST:
            await self._request(client, d)
        else:
            self.errors.append(f"client {client.index}: unexpected op {op}")

    async def _identify(self, client: _Client, d: dict[str, Any]) -> None:
        self.identifies.append(dict(d))
        if self.password is not None and d.get("authentication") != auth_string(
            self.password, client.salt, client.challenge
        ):
            self.auth_failures += 1
            await client.conn.close(AUTH_FAILED, "Authentication failed.")
            return
        async with self._changed:
            client.subs = int(d.get("eventSubscriptions", SUBSCRIBE_ALL))
            client.identified = True
            self._changed.notify_all()
        await self._send(client, OP_IDENTIFIED, {"negotiatedRpcVersion": RPC_VERSION})

    async def _request(self, client: _Client, d: dict[str, Any]) -> None:
        request_type, request_id = d["requestType"], d["requestId"]
        request_data = dict(d.get("requestData") or {})
        self.requests.append(RecordedRequest(client.index, request_type, request_data))
        if request_type in self._stalled:
            return
        queue = self._replies.get(request_type)
        if queue:
            reply = queue.pop(0) if len(queue) > 1 else queue[0]
        else:
            reply = Reply(self.version) if request_type == "GetVersion" else Reply()
        await self._send(client, OP_RESPONSE, _response(request_type, request_id, reply))

    async def _send(self, client: _Client, op: int, d: Mapping[str, Any]) -> None:
        with contextlib.suppress(ConnectionClosed):
            await client.conn.send(json.dumps({"op": op, "d": d}))

    async def _close(self, client: _Client | None, code: int | None, reason: str) -> None:
        if client is None:
            return
        if code in _UNSENDABLE_CLOSE:
            client.conn.transport.abort()
        else:
            await client.conn.close(code, reason)


def _response(request_type: str, request_id: Any, reply: Reply) -> dict[str, Any]:
    status: dict[str, Any] = {"result": reply.code == SUCCESS, "code": reply.code}
    if reply.comment:
        status["comment"] = reply.comment
    d: dict[str, Any] = {"requestType": request_type, "requestId": request_id, "requestStatus": status}
    if reply.data is not None:
        d["responseData"] = dict(reply.data)
    return d
```

- [ ] **Step 4: Run the tests to see them pass**

Run: `cd /home/light/Projects/anki_miner_game/.worktrees/t12-obs-gateway && ./.venv/bin/pytest tests/fakes/test_fake_obs_server.py -n0 -p no:cacheprovider -q`
Expected: `10 passed`.

- [ ] **Step 5: Format, lint, commit**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t12-obs-gateway && ./.venv/bin/black tests/fakes/fake_obs_server.py tests/fakes/test_fake_obs_server.py && ./.venv/bin/ruff check tests/fakes/fake_obs_server.py tests/fakes/test_fake_obs_server.py
git -C /home/light/Projects/anki_miner_game/.worktrees/t12-obs-gateway add tests/fakes/fake_obs_server.py tests/fakes/test_fake_obs_server.py
git -C /home/light/Projects/anki_miner_game/.worktrees/t12-obs-gateway commit -m "test(fakes): add an obs-websocket v5 FakeObsServer"
```

---

## Task 2: transcripts: loader, sample, replay

**Files:**
- Create: `tests/fakes/obs_transcript_sample.py`
- Modify: `tests/fakes/test_fake_obs_server.py` (imports, new section at the end)
- Modify: `tests/fakes/fake_obs_server.py` (eight edits below)

The recorder's format (`tools/obs_transcript_recorder.py` on `feat/s2-m0-tooling`, read with
`git -C /home/light/Projects/anki_miner_game show feat/s2-m0-tooling:tools/obs_transcript_recorder.py`):
one JSON object per line, `{"t_mono", "conn", "event": "open", "subprotocol", "upstream"}`,
`{"t_mono", "conn", "dir": "client->obs" | "obs->client", "msg": <frame, auth redacted>}`,
`{"t_mono", "conn", "event": "close", "by": "client" | "obs", "code", "reason"}` and
`{"t_mono", "conn", "event": "upstream_failed", "error"}`; R2 adds a `synthetic` label to edited
records. The replay rules are decision 13.

- [ ] **Step 1: Write the sample transcript**

`tests/fakes/obs_transcript_sample.py`:

```python
"""A hand-written transcript in ``tools/obs_transcript_recorder.py``'s format, for FakeObsServer's own tests.

Synthetic: every record says so in its ``synthetic`` field. The recorded R2 sessions in
``tests/fixtures/obs_transcripts/`` are the real thing; this one exists so that the loader and the
replay rules are pinned by a file whose every line is known. It holds, in order: a request client
(conn 1) and an event client (conn 2); an event right after the event client identifies; a
``GetVersion`` exchange; a recording start with an Inputs event among the Outputs events; a second
request client (conn 3) polling beside the first and getting 207; a scene collection switch; a 207
run on the main client; a failed request without data; the recorded client dropping both
connections and reconnecting (conns 4 and 5); ``ExitStarted`` without ``eventData``; OBS closing
both connections; a refused reconnect (conn 6).
"""

import json
from collections.abc import Iterable
from itertools import count
from pathlib import Path
from typing import Any

from anki_miner_game.models.obs import REQUIRED_REQUESTS

LABEL = "hand-written for tests/fakes/test_fake_obs_server.py"
UPSTREAM = "ws://127.0.0.1:4455"
SAMPLE_VERSION: dict[str, Any] = {
    "obsVersion": "32.2.2",
    "obsWebSocketVersion": "5.7.4",
    "rpcVersion": 1,
    "availableRequests": [*REQUIRED_REQUESTS, "GetOutputSettings"],
    "platform": "linux",
}
RECORDING_PATH = "/videos/_incoming/2026-09-21 18-40-39.mkv"
_ids = count(100)


def _open(conn: int, subs: int) -> list[dict[str, Any]]:
    return [
        {"conn": conn, "event": "open", "subprotocol": None, "upstream": UPSTREAM},
        {
            "conn": conn,
            "dir": "obs->client",
            "msg": {
                "op": 0,
                "d": {
                    "authentication": {"challenge": "<redacted>", "salt": "<redacted>"},
                    "obsStudioVersion": "32.2.2",
                    "obsWebSocketVersion": "5.7.4",
                    "rpcVersion": 1,
                },
            },
        },
        {
            "conn": conn,
            "dir": "client->obs",
            "msg": {"op": 1, "d": {"rpcVersion": 1, "eventSubscriptions": subs, "authentication": "<redacted>"}},
        },
        {"conn": conn, "dir": "obs->client", "msg": {"op": 2, "d": {"negotiatedRpcVersion": 1}}},
    ]


def _exchange(
    conn: int,
    request_type: str,
    request_data: dict[str, Any] | None = None,
    *,
    data: dict[str, Any] | None = None,
    code: int = 100,
    comment: str | None = None,
) -> list[dict[str, Any]]:
    request_id = next(_ids)
    request: dict[str, Any] = {"requestType": request_type, "requestId": request_id}
    if request_data:
        request["requestData"] = request_data
    status: dict[str, Any] = {"code": code, "result": code == 100}
    if comment:
        status["comment"] = comment
    response: dict[str, Any] = {"requestId": request_id, "requestStatus": status, "requestType": request_type}
    if data is not None:
        response["responseData"] = data
    return [
        {"conn": conn, "dir": "client->obs", "msg": {"op": 6, "d": request}},
        {"conn": conn, "dir": "obs->client", "msg": {"op": 7, "d": response}},
    ]


def _event(conn: int, event_type: str, intent: int, data: dict[str, Any] | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {"eventIntent": intent, "eventType": event_type}
    if data is not None:
        d["eventData"] = data
    return {"conn": conn, "dir": "obs->client", "msg": {"op": 5, "d": d}}


def _close(conn: int, by: str, code: int, reason: str = "") -> dict[str, Any]:
    return {"conn": conn, "event": "close", "by": by, "code": code, "reason": reason}


def _record(state: str, active: bool, path: str | None) -> dict[str, Any]:
    return {"outputActive": active, "outputPath": path, "outputState": f"OBS_WEBSOCKET_OUTPUT_{state}"}


_NOT_READY = {"code": 207, "comment": "OBS is not ready to perform the request."}
_SWITCH_REQUEST, _SWITCH_RESPONSE = _exchange(
    1, "SetCurrentSceneCollection", {"sceneCollectionName": "Anki Miner Game"}
)

SAMPLE_RECORDS: list[dict[str, Any]] = [
    *_open(1, 0),
    *_open(2, 2047),
    _event(2, "CurrentProfileChanged", 2, {"profileName": "Anki Miner Game"}),
    *_exchange(1, "GetVersion", data=SAMPLE_VERSION),
    *_exchange(1, "GetRecordStatus", data={"outputActive": False, "outputDuration": 0, "outputPaused": False}),
    *_exchange(1, "StartRecord"),
    _event(2, "RecordStateChanged", 64, _record("STARTING", False, None)),
    _event(2, "InputCreated", 8, {"inputName": "Game capture", "inputKind": "xcomposite_input"}),
    _event(2, "RecordStateChanged", 64, _record("STARTED", True, RECORDING_PATH)),
    *_open(3, 0),
    *_exchange(3, "GetRecordStatus", **_NOT_READY),
    _SWITCH_REQUEST,
    _event(2, "CurrentSceneCollectionChanging", 2, {"sceneCollectionName": "Untitled"}),
    _event(2, "CurrentSceneCollectionChanged", 2, {"sceneCollectionName": "Anki Miner Game"}),
    _SWITCH_RESPONSE,
    _close(3, "client", 1000),
    *_exchange(1, "GetStreamStatus", **_NOT_READY),
    *_exchange(1, "GetStreamStatus", **_NOT_READY),
    *_exchange(1, "GetStreamStatus", data={"outputActive": False}),
    *_exchange(1, "GetReplayBufferStatus", code=604, comment="Replay buffer is not available."),
    *_exchange(1, "CreateSceneCollection", {"sceneCollectionName": "Anki Miner Game"}, code=601),
    _close(1, "client", 1000),
    _close(2, "client", 1000),
    *_open(4, 0),
    *_open(5, 2047),
    *_exchange(4, "GetRecordStatus", data={"outputActive": True, "outputPaused": False, "outputDuration": 5000}),
    _event(5, "ExitStarted", 1),
    _close(4, "obs", 1001, "Server stopping."),
    _close(5, "obs", 1001, "Server stopping."),
    {"conn": 6, "event": "upstream_failed", "error": "ConnectionRefusedError: [Errno 111] Connect call failed"},
]


def write_transcript(path: Path, records: Iterable[dict[str, Any]]) -> Path:
    """Write ``records`` as recorder JSONL: ``t_mono`` increasing by 10 ms, each labelled synthetic."""
    lines = [
        json.dumps({"t_mono": 5000.0 + 0.01 * index, **record, "synthetic": LABEL}, ensure_ascii=False)
        for index, record in enumerate(records)
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_sample_transcript(directory: Path) -> Path:
    return write_transcript(directory / "sample.jsonl", SAMPLE_RECORDS)
```

- [ ] **Step 2: Write the failing tests**

In `tests/fakes/test_fake_obs_server.py`, replace the import block

```python
from tests.fakes.fake_obs_server import (
    AUTH_FAILED,
    NOT_IDENTIFIED,
    FakeObsServer,
    RecordedRequest,
    Reply,
    auth_string,
    version_data,
)
```

with

```python
from tests.fakes.fake_obs_server import (
    AUTH_FAILED,
    NOT_IDENTIFIED,
    FakeObsServer,
    RecordedRequest,
    Reply,
    auth_string,
    load_transcript,
    version_data,
)
from tests.fakes.obs_transcript_sample import (
    RECORDING_PATH,
    SAMPLE_RECORDS,
    SAMPLE_VERSION,
    write_sample_transcript,
    write_transcript,
)
```

and append at the end of the file:

```python


# --- transcripts ----------------------------------------------------------------------------------


def test_the_loader_pairs_connections_and_drops_other_clients_and_get_version(tmp_path):
    transcript = load_transcript(write_sample_transcript(tmp_path))

    assert transcript.generations == ((1, 2), (4, 5))
    assert {conn for step in transcript.steps for conn in [step.conn]} == {1, 2, 4, 5}
    assert transcript.version == SAMPLE_VERSION
    assert not any(step.d.get("requestType") == "GetVersion" for step in transcript.steps)
    assert [(e.generation, e.request_type, e.response["requestStatus"]["code"]) for e in transcript.exchanges()] == [
        (0, "GetRecordStatus", 100),
        (0, "StartRecord", 100),
        (0, "SetCurrentSceneCollection", 100),
        (0, "GetStreamStatus", 100),
        (0, "GetReplayBufferStatus", 604),
        (0, "CreateSceneCollection", 601),
        (1, "GetRecordStatus", 100),
    ]
    assert transcript.expected_events(64, "C", "L") == [
        ("C", {}),
        (
            "RecordStateChanged",
            {"outputActive": False, "outputPath": None, "outputState": "OBS_WEBSOCKET_OUTPUT_STARTING"},
        ),
        (
            "RecordStateChanged",
            {"outputActive": True, "outputPath": RECORDING_PATH, "outputState": "OBS_WEBSOCKET_OUTPUT_STARTED"},
        ),
        ("L", {}),
        ("C", {}),
        ("L", {}),
    ]


def test_the_loader_refuses_overlapping_connections(tmp_path):
    records = [r for r in SAMPLE_RECORDS if not (r.get("event") == "close" and r["conn"] in (1, 2))]

    with pytest.raises(ValueError, match="overlap"):
        load_transcript(write_transcript(tmp_path / "overlap.jsonl", records))


async def test_replay_binds_clients_by_role_and_plays_the_recording(tmp_path):
    async with FakeObsServer(transcript=load_transcript(write_sample_transcript(tmp_path))) as server:
        events = await identified(server, subs=67, password=None)  # the event client may come first
        requests = await identified(server, subs=0, password=None)

        assert (await ask(requests, "GetVersion"))["responseData"] == SAMPLE_VERSION
        profile = json.loads(await events.recv())["d"]
        status = await ask(requests, "GetRecordStatus")

        assert profile["eventType"] == "CurrentProfileChanged"
        assert status["responseData"] == {"outputActive": False, "outputDuration": 0, "outputPaused": False}
        assert (await ask(requests, "GetStreamStatus"))["requestStatus"]["code"] == 100  # not StartRecord
        assert server.unscripted and "StartRecord" in server.unscripted[0]
```

- [ ] **Step 3: Run them to see them fail**

Run: `cd /home/light/Projects/anki_miner_game/.worktrees/t12-obs-gateway && ./.venv/bin/pytest tests/fakes/test_fake_obs_server.py -n0 -p no:cacheprovider -q`
Expected: collection error, `ImportError: cannot import name 'load_transcript' from 'tests.fakes.fake_obs_server'`.

- [ ] **Step 4: Add the loader and the replay to the fake**

Edit `tests/fakes/fake_obs_server.py`:

1. Replace the standard-library imports

```python
import asyncio
import base64
import contextlib
import hashlib
import itertools
import json
import secrets
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from http import HTTPStatus
from types import TracebackType
from typing import Any, Self
```

with

```python
import asyncio
import base64
import collections
import contextlib
import hashlib
import itertools
import json
import secrets
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Self
```

2. Below the `CLOSE_TIMEOUT_S` docstring, before `_UNSENDABLE_CLOSE`, add

```python
Role = Literal["requests", "events"]
```

3. Between `RecordedRequest` and the `# --- the server ---` comment, insert the transcript section:

```python
# --- transcripts --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    conn: int
    kind: Literal["open", "request", "response", "event", "close"]
    d: Mapping[str, Any] = field(default_factory=dict)
    """The frame's ``d`` for a request, response or event."""
    by: str | None = None
    """``client`` or ``obs`` for a close."""
    code: int | None = None
    reason: str = ""


@dataclass(frozen=True)
class Exchange:
    """One request as the gateway sends it, and OBS's final answer."""

    generation: int
    request_type: str
    request_data: Mapping[str, Any]
    response: Mapping[str, Any] | None
    """The final response's ``d``; ``None`` when OBS never answered."""


@dataclass(frozen=True)
class Transcript:
    name: str
    roles: Mapping[int, Role]
    generations: tuple[tuple[int, int], ...]
    """``(requests conn, events conn)`` of each connection the recorded client made, in order."""
    steps: tuple[Step, ...]
    version: Mapping[str, Any] | None
    """The first successful recorded ``GetVersion`` answer."""

    def generation_of(self, conn: int) -> int:
        return next(g for g, pair in enumerate(self.generations) if conn in pair)

    def is_last_generation(self, conn: int) -> bool:
        return self.generation_of(conn) == len(self.generations) - 1

    def close_of(self, conn: int) -> Step | None:
        return next((s for s in self.steps if s.conn == conn and s.kind == "close"), None)

    def exchanges(self) -> list[Exchange]:
        """Every request with its final answer, in sending order.

        A run of 207 ``NotReady`` answers followed by the same request again is one exchange: the
        gateway retries 207 itself.
        """
        pending: dict[int, collections.deque[int]] = collections.defaultdict(collections.deque)
        found: list[list[Any]] = []
        for step in self.steps:
            if step.kind == "request":
                pending[step.conn].append(len(found))
                found.append([step, None])
            elif step.kind == "response":
                found[pending[step.conn].popleft()][1] = step.d
        merged: list[Exchange] = []
        for request, response in found:
            exchange = Exchange(
                self.generation_of(request.conn),
                request.d["requestType"],
                dict(request.d.get("requestData") or {}),
                response,
            )
            previous = merged[-1] if merged else None
            if (
                previous is not None
                and previous.response is not None
                and previous.response["requestStatus"]["code"] == NOT_READY
                and (previous.generation, previous.request_type, previous.request_data)
                == (exchange.generation, exchange.request_type, exchange.request_data)
            ):
                merged[-1] = exchange
            else:
                merged.append(exchange)
        return merged

    def expected_events(self, subscriptions: int, connected: str, lost: str) -> list[tuple[str, dict[str, Any]]]:
        """``(name, data)`` a gateway subscribed to ``subscriptions`` should hand on, in order.

        ``connected`` opens each generation; ``lost`` follows a connection OBS closed or one the
        recorded client dropped before reconnecting.
        """
        out: list[tuple[str, dict[str, Any]]] = []
        for _, events_conn in self.generations:
            out.append((connected, {}))
            for step in self.steps:
                if step.conn == events_conn and step.kind == "event" and step.d["eventIntent"] & subscriptions:
                    out.append((step.d["eventType"], dict(step.d.get("eventData") or {})))
            close = self.close_of(events_conn)
            if close is not None and (close.by == "obs" or not self.is_last_generation(events_conn)):
                out.append((lost, {}))
        return out


def load_transcript(path: Path) -> Transcript:
    """Read a ``tools/obs_transcript_recorder.py`` JSONL file into replay steps.

    - A connection's role comes from its Identify: ``eventSubscriptions`` 0 is a request
      connection, anything else an event connection.
    - A generation is an event connection plus the request connection opened most recently before
      it and still open (obsws-python clients connect ``ReqClient`` first). Connections outside
      every generation (a second polling client, a refused handshake) are another client's: their
      frames are dropped. Generations must not overlap in time.
    - ``GetVersion`` exchanges are dropped: the fake answers ``GetVersion`` live, with the first
      successful recorded answer, because the gateway sends its own at every connect.
    - Hello, Identify and Identified are dropped: the fake runs the handshake live, without
      authentication (the recorded authentication is redacted).
    """
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    opened: dict[int, int] = {}
    closed: dict[int, int] = {}
    roles: dict[int, Role] = {}
    for index, record in enumerate(records):
        conn = record["conn"]
        if record.get("event") == "open":
            opened[conn] = index
        elif record.get("event") == "close":
            closed[conn] = index
        elif "msg" in record and record["msg"].get("op") == OP_IDENTIFY:
            roles[conn] = "events" if record["msg"]["d"].get("eventSubscriptions", SUBSCRIBE_ALL) else "requests"
        elif "text" in record or "binary_b64" in record:
            raise ValueError(f"{path.name}: conn {conn} has a non-JSON frame, which obsws-python never sends")

    def open_at(conn: int, index: int) -> bool:
        return opened[conn] < index < closed.get(conn, len(records))

    generations: list[tuple[int, int]] = []
    for events_conn in sorted((c for c, r in roles.items() if r == "events"), key=opened.__getitem__):
        taken = {req for req, _ in generations}
        candidates = [
            c for c, r in roles.items() if r == "requests" and c not in taken and open_at(c, opened[events_conn])
        ]
        if candidates:
            generations.append((max(candidates, key=opened.__getitem__), events_conn))
    for (req_a, evt_a), (req_b, evt_b) in itertools.pairwise(generations):
        end = max(closed.get(req_a, len(records)), closed.get(evt_a, len(records)))
        if end > min(opened[req_b], opened[evt_b]):
            raise ValueError(f"{path.name}: connections {req_a}/{evt_a} and {req_b}/{evt_b} overlap")
    if not generations:
        raise ValueError(f"{path.name}: no request connection paired with an event connection")
    ours = {conn for pair in generations for conn in pair}

    dropped: set[int] = set()
    version: Mapping[str, Any] | None = None
    pending: dict[int, collections.deque[int]] = collections.defaultdict(collections.deque)
    for index, record in enumerate(records):
        msg = record.get("msg")
        if record["conn"] not in ours or msg is None:
            continue
        if msg["op"] == OP_REQUEST:
            pending[record["conn"]].append(index)
        elif msg["op"] == OP_RESPONSE:
            request_index = pending[record["conn"]].popleft()
            if msg["d"]["requestType"] == "GetVersion":
                dropped.update((request_index, index))
                if version is None and msg["d"]["requestStatus"]["result"]:
                    version = msg["d"].get("responseData")
    for leftovers in pending.values():  # never answered
        dropped.update(i for i in leftovers if records[i]["msg"]["d"]["requestType"] == "GetVersion")

    steps: list[Step] = []
    for index, record in enumerate(records):
        conn = record["conn"]
        if conn not in ours or index in dropped:
            continue
        if record.get("event") == "open":
            steps.append(Step(conn, "open"))
        elif record.get("event") == "close":
            steps.append(Step(conn, "close", by=record["by"], code=record["code"], reason=record["reason"] or ""))
        elif record["msg"]["op"] == OP_REQUEST:
            steps.append(Step(conn, "request", record["msg"]["d"]))
        elif record["msg"]["op"] == OP_RESPONSE:
            steps.append(Step(conn, "response", record["msg"]["d"]))
        elif record["msg"]["op"] == OP_EVENT:
            steps.append(Step(conn, "event", record["msg"]["d"]))
    return Transcript(path.stem, roles, tuple(generations), tuple(steps), version)
```

4. Replace the `_Client` dataclass

```python
@dataclass(eq=False)
class _Client:
    conn: ServerConnection
    index: int
    salt: str
    challenge: str
    identified: bool = False
    subs: int = 0
```

with

```python
@dataclass(eq=False)
class _Client:
    conn: ServerConnection
    index: int
    salt: str
    challenge: str
    identified: bool = False
    subs: int = 0
    bound: bool = False
    inbox: asyncio.Queue[tuple[Any, str, dict[str, Any]]] = field(default_factory=asyncio.Queue)
    """Replay: requests waiting for the step that answers them."""
    pending: collections.deque[Any] = field(default_factory=collections.deque)
    """Replay: ids of the requests taken by a step, oldest first."""

    @property
    def role(self) -> Role:
        return "events" if self.subs else "requests"
```

5. Replace `FakeObsServer.__init__`

```python
    def __init__(
        self,
        *,
        password: str | None = None,
        version: Mapping[str, Any] | None = None,
    ) -> None:
        self.password = password
        """Checked at every Identify; ``None`` = authentication off. Change it to refuse later logins."""
        self.version: dict[str, Any] = dict(version) if version is not None else version_data()
        self.refuse_connections = False
        """``True``: every handshake gets HTTP 503, as a closed OBS refuses it."""
        self.handshakes = 0
        """Handshake attempts, refused ones included."""
        self.identifies: list[dict[str, Any]] = []
        """Every Identify ``d`` received, in order."""
        self.auth_failures = 0
        self.requests: list[RecordedRequest] = []
        """Every request received, ``GetVersion`` included, in order."""
        self.errors: list[str] = []
        """Protocol violations by a client."""
        self._replies: dict[str, list[Reply]] = {}
        self._stalled: set[str] = set()
        self._ids = itertools.count(1)
        self._clients: list[_Client] = []
        self._changed = asyncio.Condition()
        self._server: Server | None = None
```

with

```python
    def __init__(
        self,
        *,
        password: str | None = None,
        version: Mapping[str, Any] | None = None,
        transcript: Transcript | None = None,
    ) -> None:
        self.password = password
        """Checked at every Identify; ``None`` = authentication off. Change it to refuse later logins."""
        if version is None:
            version = transcript.version if transcript is not None and transcript.version else version_data()
        self.version: dict[str, Any] = dict(version)
        self.transcript = transcript
        self.refuse_connections = False
        """``True``: every handshake gets HTTP 503, as a closed OBS refuses it."""
        self.handshakes = 0
        """Handshake attempts, refused ones included."""
        self.identifies: list[dict[str, Any]] = []
        """Every Identify ``d`` received, in order."""
        self.auth_failures = 0
        self.requests: list[RecordedRequest] = []
        """Every request received, ``GetVersion`` included, in order."""
        self.errors: list[str] = []
        """Protocol violations by a client."""
        self.unscripted: list[str] = []
        """Replay: requests that did not match the recording."""
        self.replay_finished = asyncio.Event()
        self.replay_position = ""
        """Replay: the step the replay is waiting on, for failure messages."""
        self._replies: dict[str, list[Reply]] = {}
        self._stalled: set[str] = set()
        self._ids = itertools.count(1)
        self._clients: list[_Client] = []
        self._changed = asyncio.Condition()
        self._server: Server | None = None
        self._replay_task: asyncio.Task[None] | None = None
```

6. Replace `start` and `stop`

```python
    async def start(self) -> None:
        self._server = await serve(
            self._handle,
            "127.0.0.1",
            0,
            process_request=self._process_request,
            ping_interval=None,
            close_timeout=CLOSE_TIMEOUT_S,
        )

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
```

with

```python
    async def start(self) -> None:
        self._server = await serve(
            self._handle,
            "127.0.0.1",
            0,
            process_request=self._process_request,
            ping_interval=None,
            close_timeout=CLOSE_TIMEOUT_S,
        )
        if self.transcript is not None:
            self._replay_task = asyncio.get_running_loop().create_task(self._replay(self.transcript))

    async def stop(self) -> None:
        if self._replay_task is not None:
            self._replay_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._replay_task
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
```

7. In `_request`, replace

```python
        if request_type in self._stalled:
            return
```

with

```python
        if request_type in self._stalled:
            return
        if self.transcript is not None and request_type != "GetVersion":
            await client.inbox.put((request_id, request_type, request_data))
            return
```

8. Between `_close` and the module-level `_response`, add the replay section:

```python
    # replay

    async def _take_unbound(self, role: Role) -> _Client:
        """The earliest identified client of ``role`` not yet bound to a recorded connection."""

        def first() -> _Client | None:
            return next((c for c in self._clients if c.identified and not c.bound and c.role == role), None)

        async with self._changed:
            await self._changed.wait_for(lambda: first() is not None)
            client = first()
            assert client is not None
            client.bound = True
            return client

    async def _replay(self, transcript: Transcript) -> None:
        """Play the recorded steps in order.

        - ``open``: bind the next identified client of that connection's role.
        - ``request``: wait until the bound client sends it. A repeat of a request whose recorded
          answer was 207 gets that answer again (the client retried longer than the recording).
        - ``response``: send the recorded answer with the client's request id.
        - ``event``: send it when the client's subscriptions cover its ``eventIntent``.
        - ``close`` by OBS: close with the recorded code (abort for 1006); after the last
          generation, refuse further handshakes as a gone OBS does. ``close`` by the recorded
          client with a later generation: cut the connection, since that client reconnected.
        """
        bound: dict[int, _Client] = {}
        asked: dict[int, tuple[str, dict[str, Any]]] = {}
        not_ready: dict[int, tuple[tuple[str, dict[str, Any]], Mapping[str, Any]]] = {}
        for number, step in enumerate(transcript.steps):
            self.replay_position = f"{transcript.name} step {number}: {step.kind} on conn {step.conn} {dict(step.d)}"
            if step.kind == "open":
                bound[step.conn] = await self._take_unbound(transcript.roles[step.conn])
                continue
            client = bound[step.conn]
            if step.kind == "request":
                wanted = (step.d["requestType"], dict(step.d.get("requestData") or {}))
                while True:
                    request_id, request_type, request_data = await client.inbox.get()
                    got = (request_type, request_data)
                    repeat = not_ready.get(step.conn)
                    if got != wanted and repeat is not None and got == repeat[0]:
                        await self._send(client, OP_RESPONSE, {**repeat[1], "requestId": request_id})
                        continue
                    if got != wanted:
                        self.unscripted.append(f"{self.replay_position}: got {request_type} {request_data}")
                    break
                client.pending.append(request_id)
                asked[step.conn] = wanted
            elif step.kind == "response":
                await self._send(client, OP_RESPONSE, {**step.d, "requestId": client.pending.popleft()})
                if step.d["requestStatus"]["code"] == NOT_READY:
                    not_ready[step.conn] = (asked[step.conn], step.d)
                else:
                    not_ready.pop(step.conn, None)
            elif step.kind == "event":
                if client.subs & step.d["eventIntent"]:
                    await self._send(client, OP_EVENT, step.d)
            elif step.by == "obs":
                if transcript.is_last_generation(step.conn):
                    self.refuse_connections = True
                await self._close(client, step.code, step.reason)
            elif not transcript.is_last_generation(step.conn):
                client.conn.transport.abort()
        self.replay_position = f"{transcript.name}: finished"
        self.replay_finished.set()
```

- [ ] **Step 5: Run the tests to see them pass**

Run: `cd /home/light/Projects/anki_miner_game/.worktrees/t12-obs-gateway && ./.venv/bin/pytest tests/fakes/test_fake_obs_server.py -n0 -p no:cacheprovider -q`
Expected: `13 passed`.

- [ ] **Step 6: Format, lint, commit**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t12-obs-gateway && ./.venv/bin/black tests/fakes/fake_obs_server.py tests/fakes/obs_transcript_sample.py tests/fakes/test_fake_obs_server.py && ./.venv/bin/ruff check tests/fakes/fake_obs_server.py tests/fakes/obs_transcript_sample.py tests/fakes/test_fake_obs_server.py
git -C /home/light/Projects/anki_miner_game/.worktrees/t12-obs-gateway add tests/fakes/fake_obs_server.py tests/fakes/obs_transcript_sample.py tests/fakes/test_fake_obs_server.py
git -C /home/light/Projects/anki_miner_game/.worktrees/t12-obs-gateway commit -m "test(fakes): replay recorded OBS transcripts in FakeObsServer"
```

---

## Task 3: the gateway: connect, requests, events, close

**Files:**
- Create: `tests/obs/__init__.py` (empty), `tests/obs/helpers.py`, `tests/obs/conftest.py`
- Create: `tests/obs/test_client.py`
- Create: `anki_miner_game/obs/client.py`

Everything of the gateway except reconnecting (Task 4). What each test group pins:

| Group | Card / spec item |
|---|---|
| conformance | `ObsGateway` Protocol (contract) |
| connect | `ObsInfo` check names the missing request (spec 11.1); 207 retried until a timeout, then `ObsRequestError`, in `connect()` (S1 summary 5, 11); `_Connected` after connect (`ObsEventName`); `ObsConfigError` passes through; subscriptions 0 and 67 |
| authentication | "on auth failure re-read once, then a banner" (spec 17): re-read once, second refusal `ObsAuthError`; password never logged (Global Constraint, S1 summary 16) |
| requests | `run_in_executor` on one worker (the loop stays free; concurrent requests never cross, S1 summary 16); `responseData` or `{}`; `ObsRequestError(request, code, comment)`; 207 in `request()` |
| scene collection changes | "requests wait while `collection_changing`" (spec 11.2, 3.3); a lost `Changed` cannot hold them forever; a lost connection ends the change |
| events | events stamped with the injected `now` on the library's thread and handed on raw (spec 4.2, 11.2); only the subscribed categories; a failing handler stops nothing |
| close | `close()` sends no `_ConnectionLost` (`docs/contracts.md` row `ObsGateway.close()`); the gateway connects again after it |

`FakeClock.sleep` moves `now` on by the requested time, so 30 s of 207 retries take a few
milliseconds; `FakeClock(advance=False)` keeps `now` still for the one test that must wait for an
event; `park_from=1.0` parks the reconnect backoff (Task 4). `make_gateway` closes every gateway at
teardown so no obsws-python thread outlives its test.

- [ ] **Step 1: Write the helpers and fixtures**

Create the empty `tests/obs/__init__.py`:

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t12-obs-gateway && : > tests/obs/__init__.py
```

`tests/obs/helpers.py`:

```python
"""Shared pieces of the OBS gateway tests: credentials, clocks, event capture."""

import asyncio
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from anki_miner_game.models.messages import ObsEvent
from anki_miner_game.models.obs import ObsCredentials, ObsEventName
from tests.fakes.fake_obs_server import FakeObsServer

PASSWORD = "gateway-test-pw"
CONNECTED = ObsEventName.CONNECTED.value
LOST = ObsEventName.CONNECTION_LOST.value


class Credentials:
    """``ObsClient``'s credentials callable for ``server``: counts calls, hands out ``passwords`` in turn (the last repeats)."""

    def __init__(self, server: FakeObsServer, *passwords: str | None) -> None:
        self.server = server
        self.passwords = list(passwords) if passwords else [PASSWORD]
        self.calls = 0

    def __call__(self) -> ObsCredentials:
        password = self.passwords[min(self.calls, len(self.passwords) - 1)]
        self.calls += 1
        return ObsCredentials("127.0.0.1", self.server.port, password)


class FakeClock:
    """Injected ``now`` and ``sleep``: ``sleep`` records its argument and moves ``now`` on by it.

    ``advance=False`` keeps ``now`` still (a wait that never times out). ``park_from`` parks every
    sleep of at least that many seconds (the reconnect backoff) until ``release()``.
    """

    def __init__(self, *, advance: bool = True, park_from: float | None = None) -> None:
        self.t = 1000.0
        self.advance = advance
        self.park_from = park_from
        self.sleeps: list[float] = []
        self._released = asyncio.Event()

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if self.park_from is not None and seconds >= self.park_from:
            await self._released.wait()
        if self.advance:
            self.t += seconds
        await asyncio.sleep(0.001)

    def release(self) -> None:
        self.park_from = None
        self._released.set()

    def backoffs(self) -> list[float]:
        return [s for s in self.sleeps if s >= 1.0]


class ThreadClock:
    """``now`` that records the thread of every call and returns the call's number."""

    def __init__(self) -> None:
        self.threads: list[threading.Thread] = []

    def now(self) -> float:
        self.threads.append(threading.current_thread())
        return float(len(self.threads))


@dataclass
class Events:
    """A subscribed handler: every ``ObsEvent`` it was given, in order."""

    got: list[ObsEvent] = field(default_factory=list)

    def __call__(self, event: ObsEvent) -> None:
        self.got.append(event)

    def names(self) -> list[str]:
        return [event.name for event in self.got]

    def count(self, name: str) -> int:
        return self.names().count(name)


async def wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)
```

`tests/obs/conftest.py`:

```python
"""Fixtures for the OBS gateway tests: a live FakeObsServer and gateways closed at teardown."""

from collections.abc import AsyncIterator, Callable

import pytest

from anki_miner_game.models.obs import ObsCredentials
from anki_miner_game.obs.client import ObsClient
from tests.fakes.fake_obs_server import FakeObsServer
from tests.obs.helpers import PASSWORD, Credentials, Events, FakeClock

MakeGateway = Callable[..., tuple[ObsClient, Events]]


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch):
    """websocket-client routes even 127.0.0.1 through ``http_proxy`` unless ``no_proxy`` lists it."""
    for var in ("http_proxy", "HTTP_PROXY"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
async def obs_server() -> AsyncIterator[FakeObsServer]:
    async with FakeObsServer(password=PASSWORD) as server:
        yield server


@pytest.fixture
async def make_gateway(obs_server: FakeObsServer) -> AsyncIterator[MakeGateway]:
    """``make_gateway(credentials=None, clock=None, now=None)`` -> a subscribed gateway; all are closed at teardown."""
    made: list[ObsClient] = []

    def make(
        credentials: Callable[[], ObsCredentials] | None = None,
        clock: FakeClock | None = None,
        now: Callable[[], float] | None = None,
    ) -> tuple[ObsClient, Events]:
        clock = clock or FakeClock()
        gateway = ObsClient(credentials or Credentials(obs_server), now=now or clock.now, sleep=clock.sleep)
        events = Events()
        gateway.subscribe(events)
        made.append(gateway)
        return gateway, events

    yield make
    for gateway in made:
        await gateway.close()
```

- [ ] **Step 2: Write the failing tests**

`tests/obs/test_client.py`:

```python
"""The OBS gateway against a live FakeObsServer (spec 3.3, 11.2, 17 auth row; docs/m0/source-findings.md 5, 8, 11)."""

import asyncio
import logging
import threading

import pytest

from anki_miner_game.interfaces.obs import ObsGateway
from anki_miner_game.models.obs import (
    REQUIRED_REQUESTS,
    ObsAuthError,
    ObsConfigError,
    ObsConnectError,
    ObsCredentials,
    ObsRequestError,
    ObsUnsupportedError,
)
from anki_miner_game.obs.client import EVENT_SUBSCRIPTIONS, NOT_READY_RETRY_S, NOT_READY_TIMEOUT_S, ObsClient
from tests.fakes.fake_obs_server import NOT_READY_COMMENT, NOT_READY_REPLY, Reply, version_data
from tests.obs.helpers import CONNECTED, LOST, PASSWORD, Credentials, FakeClock, ThreadClock, wait_until
from tests.test_contracts import _assert_conforms

RECORDING = {
    "outputActive": True,
    "outputState": "OBS_WEBSOCKET_OUTPUT_STARTED",
    "outputPath": "/videos/_incoming/2026-09-21 18-40-39.mkv",
}


def test_the_client_conforms_to_the_obs_gateway_protocol():
    _assert_conforms(ObsGateway, ObsClient)


# --- connect -------------------------------------------------------------------------------------


async def test_connect_returns_the_version_and_announces_the_connection(obs_server, make_gateway):
    clock = FakeClock()
    gateway, events = make_gateway(clock=clock)

    info = await gateway.connect()

    assert (info.obs_version, info.websocket_version) == ("32.2.2", "5.7.4")
    assert info.available_requests == frozenset(REQUIRED_REQUESTS)
    assert [(e.name, dict(e.data), e.t_mono) for e in events.got] == [(CONNECTED, {}, clock.t)]
    assert [identify["eventSubscriptions"] for identify in obs_server.identifies] == [0, EVENT_SUBSCRIPTIONS]
    assert EVENT_SUBSCRIPTIONS == 1 | 2 | 64
    assert obs_server.request_types() == ["GetVersion"]


async def test_connect_when_connected_returns_the_same_info_without_reconnecting(obs_server, make_gateway):
    gateway, events = make_gateway()
    first = await gateway.connect()

    assert await gateway.connect() is first
    assert obs_server.handshakes == 2
    assert events.names() == [CONNECTED]


async def test_connect_names_the_missing_request_and_closes(obs_server, make_gateway):
    obs_server.version = version_data(
        obs_version="29.1.3", available_requests=[r for r in REQUIRED_REQUESTS if r != "SetRecordDirectory"]
    )
    gateway, events = make_gateway()

    with pytest.raises(ObsUnsupportedError) as raised:
        await gateway.connect()

    assert (raised.value.obs_version, raised.value.missing) == ("29.1.3", ("SetRecordDirectory",))
    assert events.got == []
    await obs_server.wait_for_client_count(0)


async def test_connect_retries_not_ready_until_obs_has_loaded(obs_server, make_gateway):
    obs_server.set_reply("GetVersion", NOT_READY_REPLY, NOT_READY_REPLY, Reply(version_data()))
    clock = FakeClock()
    gateway, events = make_gateway(clock=clock)

    await gateway.connect()

    assert obs_server.request_types() == ["GetVersion"] * 3
    assert clock.sleeps == [NOT_READY_RETRY_S] * 2
    assert events.names() == [CONNECTED]


async def test_connect_raises_not_ready_after_the_timeout(obs_server, make_gateway):
    obs_server.set_reply("GetVersion", NOT_READY_REPLY)
    clock = FakeClock()
    gateway, events = make_gateway(clock=clock)
    start = clock.t

    with pytest.raises(ObsRequestError) as raised:
        await gateway.connect()

    assert (raised.value.request, raised.value.code, raised.value.comment) == ("GetVersion", 207, NOT_READY_COMMENT)
    assert clock.t - start >= NOT_READY_TIMEOUT_S
    assert events.got == []
    await obs_server.wait_for_client_count(0)


async def test_connect_fails_when_obs_is_not_listening(obs_server, make_gateway):
    obs_server.refuse_connections = True
    gateway, _ = make_gateway()

    with pytest.raises(ObsConnectError) as raised:
        await gateway.connect()

    assert type(raised.value) is ObsConnectError
    assert f"127.0.0.1:{obs_server.port}" in str(raised.value)


async def test_a_config_error_from_the_credentials_passes_through(make_gateway):
    error = ObsConfigError("OBS's websocket config.json is missing")

    def credentials() -> ObsCredentials:
        raise error

    gateway, _ = make_gateway(credentials=credentials)

    with pytest.raises(ObsConfigError) as raised:
        await gateway.connect()

    assert raised.value is error


# --- authentication (spec 17) --------------------------------------------------------------------


async def test_a_refused_password_is_read_again_once_and_the_new_one_is_used(obs_server, make_gateway):
    credentials = Credentials(obs_server, "stale-password", PASSWORD)
    gateway, events = make_gateway(credentials=credentials)

    await gateway.connect()

    assert credentials.calls == 2
    assert obs_server.auth_failures == 1
    assert events.names() == [CONNECTED]


@pytest.mark.parametrize("password", ["wrong-password", None], ids=["wrong", "missing"])
async def test_a_password_refused_twice_raises_obs_auth_error(obs_server, make_gateway, password):
    credentials = Credentials(obs_server, password)
    gateway, events = make_gateway(credentials=credentials)

    with pytest.raises(ObsAuthError):
        await gateway.connect()

    assert credentials.calls == 2
    assert obs_server.auth_failures == (2 if password else 0)
    assert events.got == []
    await obs_server.wait_for_client_count(0)


async def test_the_password_is_never_logged(obs_server, make_gateway, caplog):
    caplog.set_level(logging.DEBUG)
    gateway, _ = make_gateway(credentials=Credentials(obs_server, "wrong-password", PASSWORD))

    await gateway.connect()
    obs_server.set_reply("StartRecord", Reply(code=500, comment="Recording failed"))
    with pytest.raises(ObsRequestError):
        await gateway.request("StartRecord")
    await gateway.close()

    assert logging.getLogger("obsws_python").getEffectiveLevel() >= logging.WARNING
    assert caplog.records, "the gateway logs its own connection lines"
    for record in caplog.records:
        assert PASSWORD not in record.getMessage()
        assert "wrong-password" not in record.getMessage()
    assert PASSWORD not in repr(gateway)


# --- requests ------------------------------------------------------------------------------------


async def test_request_sends_the_fields_and_returns_the_response_data(obs_server, make_gateway):
    obs_server.set_reply("GetRecordStatus", Reply({"outputActive": False, "outputDuration": 0}))
    gateway, _ = make_gateway()
    await gateway.connect()

    assert await gateway.request("GetRecordStatus") == {"outputActive": False, "outputDuration": 0}
    assert await gateway.request("SetCurrentProfile", profileName="Anki Miner Game") == {}

    assert [(r.request_type, dict(r.request_data)) for r in obs_server.requests[1:]] == [
        ("GetRecordStatus", {}),
        ("SetCurrentProfile", {"profileName": "Anki Miner Game"}),
    ]


@pytest.mark.parametrize(
    ("reply", "comment"),
    [
        (Reply(code=604, comment="Replay buffer is not available."), "Replay buffer is not available."),
        (Reply(code=601), ""),
    ],
    ids=["with-comment", "without-comment"],
)
async def test_a_failed_request_raises_obs_request_error(obs_server, make_gateway, reply, comment):
    obs_server.set_reply("GetReplayBufferStatus", reply)
    gateway, events = make_gateway()
    await gateway.connect()

    with pytest.raises(ObsRequestError) as raised:
        await gateway.request("GetReplayBufferStatus")

    assert (raised.value.request, raised.value.code, raised.value.comment) == (
        "GetReplayBufferStatus",
        reply.code,
        comment,
    )
    assert events.names() == [CONNECTED]
    assert await gateway.request("GetRecordStatus") == {}


async def test_request_retries_not_ready_then_returns(obs_server, make_gateway):
    obs_server.set_reply("GetRecordStatus", NOT_READY_REPLY, NOT_READY_REPLY, Reply({"outputActive": True}))
    clock = FakeClock()
    gateway, _ = make_gateway(clock=clock)
    await gateway.connect()

    assert await gateway.request("GetRecordStatus") == {"outputActive": True}
    assert obs_server.request_types().count("GetRecordStatus") == 3
    assert clock.sleeps == [NOT_READY_RETRY_S] * 2


async def test_request_raises_not_ready_after_the_timeout(obs_server, make_gateway):
    obs_server.set_reply("GetRecordStatus", NOT_READY_REPLY)
    clock = FakeClock()
    gateway, _ = make_gateway(clock=clock)
    await gateway.connect()
    start = clock.t

    with pytest.raises(ObsRequestError) as raised:
        await gateway.request("GetRecordStatus")

    assert (raised.value.code, raised.value.comment) == (207, NOT_READY_COMMENT)
    assert clock.t - start >= NOT_READY_TIMEOUT_S


async def test_request_before_connect_raises_obs_connect_error(make_gateway):
    gateway, _ = make_gateway()

    with pytest.raises(ObsConnectError):
        await gateway.request("GetRecordStatus")


async def test_concurrent_requests_each_get_their_own_answer(obs_server, make_gateway):
    """obsws-python never matches a reply to its request, so requests must not overlap on the wire."""
    names = ["GetRecordStatus", "GetStreamStatus", "GetReplayBufferStatus", "GetVirtualCamStatus", "GetProfileList"]
    for name in names:
        obs_server.set_reply(name, Reply({"asked": name}))
    gateway, _ = make_gateway()
    await gateway.connect()

    answers = await asyncio.gather(*(gateway.request(name) for name in names * 4))

    assert [answer["asked"] for answer in answers] == names * 4


async def test_a_stalled_request_leaves_the_loop_free_and_close_ends_it(obs_server, make_gateway):
    obs_server.stall("SetCurrentProfile")
    gateway, events = make_gateway()
    await gateway.connect()
    pending = asyncio.create_task(gateway.request("SetCurrentProfile", profileName="Anki Miner Game"))

    ticks = 0
    while ticks < 20:
        await asyncio.sleep(0.005)
        ticks += 1
    assert not pending.done()

    await gateway.close()

    with pytest.raises(ObsConnectError):
        await pending
    assert events.names() == [CONNECTED]


# --- scene collection changes --------------------------------------------------------------------


async def test_collection_changing_follows_the_changing_and_changed_events(obs_server, make_gateway):
    gateway, events = make_gateway()
    await gateway.connect()

    await obs_server.emit("CurrentSceneCollectionChanging", {"sceneCollectionName": "Untitled"})
    await wait_until(lambda: gateway.collection_changing)
    await obs_server.emit("CurrentSceneCollectionChanged", {"sceneCollectionName": "Anki Miner Game"})
    await wait_until(lambda: not gateway.collection_changing)

    assert events.names() == [CONNECTED, "CurrentSceneCollectionChanging", "CurrentSceneCollectionChanged"]


async def test_requests_wait_while_the_collection_changes(obs_server, make_gateway):
    gateway, _ = make_gateway(clock=FakeClock(advance=False))
    await gateway.connect()
    await obs_server.emit("CurrentSceneCollectionChanging", {"sceneCollectionName": "Untitled"})
    await wait_until(lambda: gateway.collection_changing)

    pending = asyncio.create_task(gateway.request("GetRecordStatus"))
    await asyncio.sleep(0.1)
    assert obs_server.request_types() == ["GetVersion"]

    await obs_server.emit("CurrentSceneCollectionChanged", {"sceneCollectionName": "Anki Miner Game"})
    assert await asyncio.wait_for(pending, 5.0) == {}
    assert obs_server.request_types() == ["GetVersion", "GetRecordStatus"]


async def test_a_lost_changed_event_does_not_hold_requests_forever(obs_server, make_gateway):
    clock = FakeClock()
    gateway, _ = make_gateway(clock=clock)
    await gateway.connect()
    await obs_server.emit("CurrentSceneCollectionChanging", {"sceneCollectionName": "Untitled"})
    await wait_until(lambda: gateway.collection_changing)
    start = clock.t

    assert await gateway.request("GetRecordStatus") == {}
    assert clock.t - start >= NOT_READY_TIMEOUT_S


async def test_a_lost_connection_ends_the_collection_change(obs_server, make_gateway):
    gateway, events = make_gateway(clock=FakeClock(park_from=1.0))
    await gateway.connect()
    await obs_server.emit("CurrentSceneCollectionChanging", {"sceneCollectionName": "Untitled"})
    await wait_until(lambda: gateway.collection_changing)

    await obs_server.drop_clients()
    await wait_until(lambda: LOST in events.names())

    assert not gateway.collection_changing


# --- events --------------------------------------------------------------------------------------


async def test_events_arrive_as_obs_sent_them_stamped_with_the_injected_clock(obs_server, make_gateway):
    clock = FakeClock()
    gateway, events = make_gateway(clock=clock)
    await gateway.connect()
    clock.t = 5000.25

    await obs_server.emit("RecordStateChanged", RECORDING)
    await obs_server.emit("ExitStarted")
    await wait_until(lambda: len(events.got) == 3)

    assert [(e.name, dict(e.data), e.t_mono) for e in events.got[1:]] == [
        ("RecordStateChanged", RECORDING, 5000.25),
        ("ExitStarted", {}, 5000.25),
    ]


async def test_events_are_stamped_on_the_library_event_thread(obs_server, make_gateway):
    clock = ThreadClock()
    gateway, events = make_gateway(now=clock.now)
    await gateway.connect()

    await obs_server.emit("RecordStateChanged", RECORDING)
    await wait_until(lambda: len(events.got) == 2)

    stamped_by = clock.threads[int(events.got[1].t_mono) - 1]
    assert stamped_by is not threading.current_thread()
    assert not stamped_by.name.startswith("obs-gateway")


async def test_only_the_subscribed_categories_reach_the_handlers(obs_server, make_gateway):
    gateway, events = make_gateway()
    await gateway.connect()

    await obs_server.emit("InputCreated", {"inputName": "Game capture"})
    await obs_server.emit("SceneCreated", {"sceneName": "Game"})
    await obs_server.emit("CurrentProfileChanged", {"profileName": "Anki Miner Game"})
    await wait_until(lambda: len(events.got) == 2)

    assert events.names() == [CONNECTED, "CurrentProfileChanged"]


async def test_every_handler_gets_every_event_and_a_failing_one_stops_nothing(obs_server, make_gateway):
    gateway, events = make_gateway()
    later: list[str] = []

    def broken(event):
        raise RuntimeError("handler bug")

    gateway.subscribe(broken)
    gateway.subscribe(lambda event: later.append(event.name))
    await gateway.connect()

    await obs_server.emit("RecordStateChanged", RECORDING)
    await obs_server.emit("RecordFileChanged", {"newOutputPath": "/videos/_incoming/b.mkv"})
    await wait_until(lambda: len(later) == 3)

    assert events.names() == later == [CONNECTED, "RecordStateChanged", "RecordFileChanged"]


# --- close ---------------------------------------------------------------------------------------


async def test_close_disconnects_without_a_lost_event(obs_server, make_gateway):
    clock = FakeClock()
    gateway, events = make_gateway(clock=clock)
    await gateway.connect()

    await gateway.close()
    await obs_server.wait_for_client_count(0)
    await asyncio.sleep(0.1)

    assert events.names() == [CONNECTED]
    assert obs_server.handshakes == 2
    with pytest.raises(ObsConnectError):
        await gateway.request("GetRecordStatus")


async def test_the_gateway_connects_again_after_close(obs_server, make_gateway):
    gateway, events = make_gateway()
    await gateway.connect()
    await gateway.close()

    await gateway.connect()

    assert events.names() == [CONNECTED, CONNECTED]
    assert await gateway.request("GetRecordStatus") == {}
```

- [ ] **Step 3: Run them to see them fail**

Run: `cd /home/light/Projects/anki_miner_game/.worktrees/t12-obs-gateway && ./.venv/bin/pytest tests/obs/test_client.py -n0 -p no:cacheprovider -q`
Expected: collection error, `ModuleNotFoundError: No module named 'anki_miner_game.obs.client'`.

- [ ] **Step 4: Write the gateway**

`anki_miner_game/obs/client.py`:

```python
"""The OBS gateway: one obs-websocket v5 connection with reconnect (spec 11.2).

Built on obsws-python 1.8.0 (``ReqClient`` and ``EventClient``), whose threading and logging the
M0 source research read (``docs/m0/source-findings.md`` section 11, summary items 5, 7, 11, 16):

- ``ReqClient`` blocks, never matches a reply to its request id and cannot survive a timeout, so
  every request runs on this gateway's one worker thread (``run_in_executor``, spec 4.2) and a
  failed or timed-out request drops the whole connection.
- ``EventClient`` runs callbacks on its own reader thread, converts ``eventData`` to snake-case
  dataclasses and ends silently when the socket closes. ``_EventClient`` takes the raw frame
  instead, stamps it with the injected ``now`` on that thread (spec 4.2, 7) and reports the thread's
  end, which is how a lost connection is noticed.
- obsws-python logs the password at INFO when it connects and every failed request with a
  traceback at ERROR, so its logger is held at CRITICAL; this module logs one line of its own
  instead, never with the password.
- Until OBS has loaded, and during a scene collection change, obs-websocket answers every request
  with 207 ``NotReady`` and drops events. ``connect`` and ``request`` retry 207 until
  ``NOT_READY_TIMEOUT_S``; ``request`` also waits while a ``CurrentSceneCollectionChanging`` has no
  ``CurrentSceneCollectionChanged`` yet.

Handlers see ``_Connected`` after every successful connect, then that connection's events in
arrival order, then ``_ConnectionLost`` when it drops; events that arrive while ``connect`` is still
checking ``GetVersion`` are held and delivered right after ``_Connected``.
"""

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, Final, NoReturn, TypeVar

import obsws_python as obsws
from obsws_python.error import OBSSDKError

from anki_miner_game.models.messages import ObsEvent
from anki_miner_game.models.obs import (
    ObsAuthError,
    ObsConnectError,
    ObsCredentials,
    ObsEventName,
    ObsInfo,
    ObsRequestError,
    ObsUnsupportedError,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Provisional until R2 (docs/m0/obs-behaviour.md): how long OBS takes to load and to switch a scene
# collection, and how long its slowest blocking request holds the answer.
NOT_READY_TIMEOUT_S: Final = 30.0
"""How long ``connect`` and ``request`` retry 207 ``NotReady`` (and ``request`` waits out a collection change)."""
NOT_READY_RETRY_S: Final = 0.25
"""Pause between two tries of a request OBS answered with 207."""
REQUEST_TIMEOUT_S: Final = 20.0
"""Socket timeout for connecting and for each answer; a request past it drops the connection."""

NOT_READY: Final = 207
COLLECTION_POLL_S: Final = 0.05
"""How often a waiting ``request`` checks whether the scene collection change is over."""
JOIN_TIMEOUT_S: Final = 2.0
"""How long closing a connection waits for obsws-python's event thread to end."""
EVENT_SUBSCRIPTIONS: Final = int(obsws.Subs.GENERAL | obsws.Subs.CONFIG | obsws.Subs.OUTPUTS)
"""``ExitStarted`` (General), profile and collection switches (Config), record events (Outputs)."""
LIBRARY_LOG_LEVEL: Final = logging.CRITICAL


class ObsClient:
    """The ``ObsGateway`` (``interfaces/obs.py``) on obsws-python.

    ``credentials`` is called before every connection attempt, on the gateway's worker thread:
    ``app.py`` passes ``lambda: discovery.credentials(<current AppConfig>)``, so a changed OBS
    config or a typed password override is used at the next attempt. Its ``ObsConfigError`` is
    raised unchanged. ``now`` stamps events and measures the 207 and collection-change waits;
    ``sleep`` paces those waits and the reconnect backoff.

    Use it from one asyncio loop (the I/O loop). ``subscribe`` may be called from any thread;
    handlers run on obsws-python's event thread or on the loop, one at a time, and must only
    enqueue.
    """

    def __init__(
        self,
        credentials: Callable[[], ObsCredentials],
        *,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        logging.getLogger("obsws_python").setLevel(LIBRARY_LOG_LEVEL)
        self._credentials = credentials
        self._now = now
        self._sleep = sleep
        self._lock = threading.Lock()
        """Guards the handlers, ``_changing`` and every link's ``ready``/``lost``/``closed`` flags."""
        self._handlers: list[Callable[[ObsEvent], None]] = []
        self._changing = False
        self._link: _Link | None = None
        self._info: ObsInfo | None = None
        self._auth_failed = False
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._connect_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()

    # ObsGateway

    async def connect(self) -> ObsInfo:
        """Connect unless connected; see ``ObsGateway.connect``.

        Raises ``ObsConnectError`` (OBS unreachable), ``ObsAuthError`` (the password was refused
        twice, the credentials read again in between), ``ObsConfigError`` (from ``credentials``),
        ``ObsUnsupportedError`` (a required request is missing) or ``ObsRequestError`` (207 past
        the timeout).
        """
        self._loop = asyncio.get_running_loop()
        self._closed = False
        async with self._connect_lock:
            if self._link is not None and not self._link.lost and self._info is not None:
                return self._info
            return await self._connect_once()

    async def request(self, name: str, **fields: Any) -> dict[str, Any]:
        """See ``ObsGateway.request``. While reconnecting after a refused password it raises ``ObsAuthError``."""
        deadline = self._now() + NOT_READY_TIMEOUT_S
        while self._changing and self._now() < deadline:
            await self._sleep(COLLECTION_POLL_S)
        return await self._call(self._require_link(), name, fields, deadline)

    def subscribe(self, handler: Callable[[ObsEvent], None]) -> None:
        with self._lock:
            self._handlers.append(handler)

    @property
    def collection_changing(self) -> bool:
        return self._changing

    async def close(self) -> None:
        """Disconnect and stop reconnecting; no ``_ConnectionLost`` follows. ``connect`` may be called again."""
        self._closed = True
        link, self._link = self._link, None
        if link is not None:
            with self._lock:
                link.closed = True
                self._changing = False
            await self._teardown(link)
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._executor is not None:
            self._executor.shutdown(wait=False)
            self._executor = None

    # connecting

    async def _connect_once(self) -> ObsInfo:
        link = await self._open_authenticated()
        try:
            info = await self._check_version(link)
            self._mark_ready(link)
        except BaseException:
            await self._teardown(link)
            raise
        self._info = info
        logger.info("connected to OBS %s (obs-websocket %s)", info.obs_version, info.websocket_version)
        return info

    async def _open_authenticated(self) -> "_Link":
        """Open both connections; on a refused password read the credentials again and retry once (spec 17)."""
        try:
            return await self._open()
        except _AuthRefusedError:
            logger.info("OBS refused the websocket password; reading OBS's websocket settings again")
        try:
            return await self._open()
        except _AuthRefusedError:
            self._auth_failed = True
            raise ObsAuthError("OBS refused the websocket password") from None

    async def _open(self) -> "_Link":
        credentials = await self._run(self._credentials)
        link = _Link()
        await self._run(link.open, credentials, partial(self._on_event, link), partial(self._on_end, link))
        return link

    async def _check_version(self, link: "_Link") -> ObsInfo:
        data = await self._call(link, "GetVersion", {}, self._now() + NOT_READY_TIMEOUT_S)
        info = ObsInfo(
            obs_version=str(data.get("obsVersion", "")),
            websocket_version=str(data.get("obsWebSocketVersion", "")),
            available_requests=frozenset(data.get("availableRequests") or ()),
        )
        missing = info.missing_requests()
        if missing:
            raise ObsUnsupportedError(info.obs_version, missing)
        return info

    def _mark_ready(self, link: "_Link") -> None:
        """Announce the connection, then hand on the events held while it was being checked."""
        with self._lock:
            if link.lost:
                raise ObsConnectError("OBS closed the connection while it was being set up")
            link.ready = True
            self._link = link
            self._auth_failed = False
            self._deliver(ObsEvent(ObsEventName.CONNECTED, {}, self._now()))
            for event in link.held:
                self._deliver(event)
            link.held.clear()

    # requests

    def _require_link(self) -> "_Link":
        link = self._link
        if link is None or link.lost:
            if self._auth_failed:
                raise ObsAuthError("OBS refused the websocket password")
            raise ObsConnectError("not connected to OBS")
        return link

    async def _call(self, link: "_Link", name: str, fields: Mapping[str, Any], deadline: float) -> dict[str, Any]:
        """Send until OBS answers something other than 207 or ``deadline`` passes."""
        while True:
            reply = await self._send(link, name, fields)
            status = reply["requestStatus"]
            if status.get("result"):
                data = reply.get("responseData")
                return dict(data) if isinstance(data, dict) else {}
            code = int(status.get("code", 0))
            if code != NOT_READY or self._now() >= deadline:
                raise ObsRequestError(name, code, str(status.get("comment") or ""))
            await self._sleep(NOT_READY_RETRY_S)

    async def _send(self, link: "_Link", name: str, fields: Mapping[str, Any]) -> dict[str, Any]:
        if link.lost or link.closed:
            raise ObsConnectError("not connected to OBS")
        try:
            return await self._run(_blocking_send, link.req, name, dict(fields))
        except Exception as exc:
            self._lose(link, f"{name}: {type(exc).__name__}: {exc}")
            raise ObsConnectError(f"lost the connection to OBS during {name}") from exc

    # events and connection loss (any thread)

    def _on_event(self, link: "_Link", name: object, data: object) -> None:
        t_mono = self._now()
        if not isinstance(name, str):
            return
        event = ObsEvent(name, data if isinstance(data, dict) else {}, t_mono)
        with self._lock:
            if link.lost or link.closed:
                return
            if not link.ready:
                link.held.append(event)
                return
            self._deliver(event)

    def _on_end(self, link: "_Link") -> None:
        self._lose(link, "the event connection closed")

    def _lose(self, link: "_Link", reason: str) -> None:
        with self._lock:
            if link.lost or link.closed:
                return
            link.lost = True
            if not link.ready:  # connect() is still setting it up and will see the flag
                return
            self._changing = False
            self._deliver(ObsEvent(ObsEventName.CONNECTION_LOST, {}, self._now()))
        logger.warning("lost the connection to OBS (%s)", reason)
        if self._loop is not None:
            with contextlib.suppress(RuntimeError):  # the loop is already closed
                self._loop.call_soon_threadsafe(self._after_loss, link)

    def _deliver(self, event: ObsEvent) -> None:
        """Hand ``event`` to every handler; the caller holds ``_lock``."""
        if event.name == ObsEventName.CURRENT_SCENE_COLLECTION_CHANGING:
            self._changing = True
        elif event.name == ObsEventName.CURRENT_SCENE_COLLECTION_CHANGED:
            self._changing = False
        for handler in self._handlers:
            try:
                handler(event)
            except Exception:
                logger.exception("an OBS event handler failed on %s", event.name)

    # after a loss (on the loop)

    def _after_loss(self, link: "_Link") -> None:
        if self._link is link:
            self._link = None
        self._spawn(self._teardown(link))

    async def _teardown(self, link: "_Link") -> None:
        link.abort()  # wakes a request blocked on this connection
        with contextlib.suppress(Exception):
            await self._run(link.shutdown)

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _run(self, fn: Callable[..., T], *args: Any) -> "asyncio.Future[T]":
        """Run blocking obsws-python work on the gateway's one worker thread."""
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="obs-gateway")
        return asyncio.get_running_loop().run_in_executor(self._executor, fn, *args)


class _AuthRefusedError(Exception):
    """OBS asked for a password and refused the Identify (close 4009), or there was no password to send."""


def _identify_failed(client: Any, exc: Exception) -> NoReturn:
    """Close the half-open socket after a failed Identify; ``_AuthRefusedError`` when OBS required a password."""
    base = client.base_client
    with contextlib.suppress(Exception):
        base.ws.shutdown()
    if "authentication" in base.server_hello.get("d", {}):
        raise _AuthRefusedError from exc
    raise exc


class _ReqClient(obsws.ReqClient):
    def __init__(self, **kwargs: Any) -> None:
        try:
            super().__init__(**kwargs)
        except OBSSDKError as exc:
            _identify_failed(self, exc)


class _RawCallback:
    """Stands in for obsws-python's ``Callback``: hands on each event's type and ``eventData`` as sent."""

    def __init__(self, fn: Callable[[object, object], None]) -> None:
        self._fn = fn

    def trigger(self, event: object, data: object) -> None:
        self._fn(event, data)


class _EventClient(obsws.EventClient):
    def __init__(self, on_event: Callable[[object, object], None], on_end: Callable[[], None], **kwargs: Any) -> None:
        self._on_event = on_event
        self._on_end = on_end
        try:
            super().__init__(**kwargs)
        except OBSSDKError as exc:
            _identify_failed(self, exc)

    def subscribe(self) -> None:
        self.callback = _RawCallback(self._on_event)  # before the reader thread starts
        super().subscribe()

    def trigger(self, stop_event: threading.Event) -> None:
        try:
            super().trigger(stop_event)
        finally:
            self._on_end()


class _Link:
    """One connection to OBS: a request client and an event client opened with the same credentials."""

    def __init__(self) -> None:
        self.req: Any = None
        self.evt: Any = None
        self.ready = False
        self.lost = False
        self.closed = False
        self.held: list[ObsEvent] = []

    def open(
        self,
        credentials: ObsCredentials,
        on_event: Callable[[object, object], None],
        on_end: Callable[[], None],
    ) -> None:
        """Blocking. Raises ``_AuthRefusedError`` or ``ObsConnectError``; leaves nothing open when it raises."""
        options = {
            "host": credentials.host,
            "port": credentials.port,
            "password": credentials.password or "",
            "timeout": REQUEST_TIMEOUT_S,
        }
        try:
            self.req = _ReqClient(**options)
            self.evt = _EventClient(on_event, on_end, subs=EVENT_SUBSCRIPTIONS, **options)
        except _AuthRefusedError:
            self.shutdown()
            raise
        except Exception as exc:
            self.shutdown()
            raise ObsConnectError(
                f"cannot connect to OBS at {credentials.host}:{credentials.port}: {type(exc).__name__}: {exc}"
            ) from exc

    def abort(self) -> None:
        """Any thread: wake whatever is blocked reading either socket."""
        for client in (self.req, self.evt):
            if client is not None:
                with contextlib.suppress(Exception):
                    client.base_client.ws.abort()

    def shutdown(self) -> None:
        """Blocking: close both sockets and wait for the event thread."""
        self.abort()
        for client in (self.req, self.evt):
            if client is not None:
                with contextlib.suppress(Exception):
                    client.base_client.ws.shutdown()
        worker = getattr(self.evt, "worker", None)
        if worker is not None and worker is not threading.current_thread():
            worker.join(JOIN_TIMEOUT_S)


def _blocking_send(req: Any, name: str, data: dict[str, Any]) -> dict[str, Any]:
    """One request on ``req``'s socket; the reply's ``d``. Raises on anything but a well-formed reply to ``name``."""
    reply = req.base_client.req(name, data or None)
    if (
        not isinstance(reply, dict)
        or reply.get("requestType") != name
        or not isinstance(reply.get("requestStatus"), dict)
    ):
        raise ValueError(f"unexpected reply to {name}")
    return reply
```

Notes on the parts a reviewer will ask about:
- `_ReqClient` and `_EventClient` subclass the obsws-python clients only to tell a refused
  password from other failures (the Hello is on `base_client.server_hello`, which the plain
  constructors lose when they raise) and, for events, to take raw frames and report the reader
  thread's end. `_blocking_send` uses `base_client.req` (decision 10).
- `_lose` runs on whichever thread notices the loss first (the event thread, or the loop after a
  failed request), delivers `_ConnectionLost` once under the lock, and hands the rest to the loop
  with `call_soon_threadsafe`. A link that was never announced is only marked lost; `connect`
  sees the flag in `_mark_ready` and fails.
- `_teardown` aborts both sockets from the loop first (`WebSocket.abort` shuts the socket down,
  which wakes a `recv` blocked on the worker thread), then closes them and joins the event
  thread on the worker, after whatever request was in flight.

- [ ] **Step 5: Run the tests to see them pass**

Run: `cd /home/light/Projects/anki_miner_game/.worktrees/t12-obs-gateway && ./.venv/bin/pytest tests/obs/test_client.py -n0 -p no:cacheprovider -q`
Expected: `30 passed`.

- [ ] **Step 6: Type-check, format, lint, commit**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t12-obs-gateway && ./.venv/bin/mypy anki_miner_game && ./.venv/bin/black anki_miner_game/obs/client.py tests/obs && ./.venv/bin/ruff check anki_miner_game/obs/client.py tests/obs
git -C /home/light/Projects/anki_miner_game/.worktrees/t12-obs-gateway add anki_miner_game/obs/client.py tests/obs/__init__.py tests/obs/helpers.py tests/obs/conftest.py tests/obs/test_client.py
git -C /home/light/Projects/anki_miner_game/.worktrees/t12-obs-gateway commit -m "feat(obs): add the OBS gateway on obsws-python"
```

mypy expected: `Success: no issues found`. The obsws-python base classes are `Any` to mypy
(`ignore_missing_imports` in `pyproject.toml`); do not add `# type: ignore` to the subclasses,
`warn_unused_ignores` would flag it.

---

## Task 4: connection loss and reconnect

**Files:**
- Modify: `tests/obs/test_client.py` (imports, new section at the end)
- Modify: `anki_miner_game/obs/client.py` (seven edits below)

Spec 11.2: "Connection loss triggers reconnect with backoff, and every successful connect runs
reconcile" (the actor reconciles on each `_Connected`); card: "credentials from
`ObsDiscovery.credentials` at every connect". Decisions 2 and 3.

- [ ] **Step 1: Write the failing tests**

In `tests/obs/test_client.py`, replace

```python
from anki_miner_game.obs.client import EVENT_SUBSCRIPTIONS, NOT_READY_RETRY_S, NOT_READY_TIMEOUT_S, ObsClient
from tests.fakes.fake_obs_server import NOT_READY_COMMENT, NOT_READY_REPLY, Reply, version_data
```

with

```python
from anki_miner_game.obs import client as client_module
from anki_miner_game.obs.client import EVENT_SUBSCRIPTIONS, NOT_READY_RETRY_S, NOT_READY_TIMEOUT_S, ObsClient
from tests.fakes.fake_obs_server import NOT_READY_COMMENT, NOT_READY_REPLY, FakeObsServer, Reply, version_data
```

and append at the end of the file:

```python


# --- connection loss and reconnect ---------------------------------------------------------------


async def test_a_failed_first_connect_starts_no_reconnecting(obs_server, make_gateway):
    obs_server.refuse_connections = True
    clock = FakeClock()
    gateway, _ = make_gateway(clock=clock)
    with pytest.raises(ObsConnectError):
        await gateway.connect()

    await asyncio.sleep(0.1)

    assert obs_server.handshakes == 1
    assert clock.backoffs() == []


async def test_the_credentials_are_read_again_at_every_connect(obs_server, make_gateway):
    """A reconnect goes wherever OBS's config says now: here a second OBS on another port."""
    async with FakeObsServer(password=PASSWORD) as other:
        target = {"server": obs_server}

        def credentials() -> ObsCredentials:
            return ObsCredentials("127.0.0.1", target["server"].port, PASSWORD)

        gateway, events = make_gateway(credentials=credentials)
        await gateway.connect()
        target["server"] = other

        await obs_server.drop_clients()
        await wait_until(lambda: events.count(CONNECTED) == 2)

        assert other.client_count == 2
        assert await gateway.request("GetRecordStatus") == {}
        assert other.request_types() == ["GetVersion", "GetRecordStatus"]
        await gateway.close()


async def test_requests_raise_obs_auth_error_while_the_password_is_refused(obs_server, make_gateway):
    clock = FakeClock()
    gateway, events = make_gateway(clock=clock)
    await gateway.connect()
    obs_server.password = "changed-in-obs"

    await obs_server.drop_clients()
    await wait_until(lambda: len(clock.backoffs()) >= 2)  # the first attempt, password read twice, is over

    with pytest.raises(ObsAuthError):
        await gateway.request("GetRecordStatus")

    obs_server.password = PASSWORD
    await wait_until(lambda: events.count(CONNECTED) == 2)
    assert await gateway.request("GetRecordStatus") == {}

    obs_server.refuse_connections = True  # a later loss is no longer about the password
    await obs_server.drop_clients()
    await wait_until(lambda: events.count(LOST) == 2)
    with pytest.raises(ObsConnectError) as raised:
        await gateway.request("GetRecordStatus")
    assert type(raised.value) is ObsConnectError


async def test_a_request_that_times_out_drops_the_connection_and_reconnects(obs_server, make_gateway, monkeypatch):
    monkeypatch.setattr(client_module, "REQUEST_TIMEOUT_S", 0.2)
    obs_server.stall("StopRecord")
    gateway, events = make_gateway()
    await gateway.connect()

    with pytest.raises(ObsConnectError):
        await gateway.request("StopRecord")

    await wait_until(lambda: events.names() == [CONNECTED, LOST, CONNECTED])
    assert await gateway.request("GetRecordStatus") == {}


@pytest.mark.parametrize(("code", "reason"), [(None, ""), (1001, "Server stopping.")], ids=["vanished", "obs-exit"])
async def test_a_dropped_connection_is_announced_and_reconnected(obs_server, make_gateway, code, reason):
    clock = FakeClock()
    gateway, events = make_gateway(clock=clock)
    await gateway.connect()

    await obs_server.drop_clients(code, reason)
    await wait_until(lambda: events.count(CONNECTED) == 2)

    assert events.names() == [CONNECTED, LOST, CONNECTED]
    assert clock.backoffs() == [1.0]
    assert await gateway.request("GetRecordStatus") == {}


async def test_reconnect_backs_off_1_2_5_10_then_10_seconds(obs_server, make_gateway):
    clock = FakeClock()
    credentials = Credentials(obs_server)
    gateway, events = make_gateway(credentials=credentials, clock=clock)
    await gateway.connect()
    obs_server.refuse_connections = True

    await obs_server.drop_clients()
    await wait_until(lambda: len(clock.backoffs()) >= 6)
    obs_server.refuse_connections = False
    await wait_until(lambda: events.count(CONNECTED) == 2)

    assert clock.backoffs()[:6] == [1.0, 2.0, 5.0, 10.0, 10.0, 10.0]
    assert credentials.calls == len(clock.backoffs()) + 1


async def test_requests_fail_fast_while_reconnecting(obs_server, make_gateway):
    clock = FakeClock(park_from=1.0)
    gateway, events = make_gateway(clock=clock)
    await gateway.connect()

    await obs_server.drop_clients()
    await wait_until(lambda: events.names() == [CONNECTED, LOST])

    with pytest.raises(ObsConnectError) as raised:
        await gateway.request("GetRecordStatus")
    assert type(raised.value) is ObsConnectError

    clock.release()
    await wait_until(lambda: events.count(CONNECTED) == 2)


async def test_connect_during_the_backoff_connects_at_once(obs_server, make_gateway):
    clock = FakeClock(park_from=1.0)
    gateway, events = make_gateway(clock=clock)
    await gateway.connect()
    await obs_server.drop_clients()
    await wait_until(lambda: events.names() == [CONNECTED, LOST])

    await gateway.connect()
    clock.release()
    await asyncio.sleep(0.1)

    assert events.names() == [CONNECTED, LOST, CONNECTED]
    assert obs_server.client_count == 2


async def test_close_during_reconnect_attempts_leaves_nothing_open(obs_server, make_gateway):
    gateway, events = make_gateway()
    await gateway.connect()
    obs_server.set_reply("GetVersion", NOT_READY_REPLY)

    await obs_server.drop_clients()
    await wait_until(lambda: obs_server.request_types().count("GetVersion") >= 3)
    await gateway.close()

    await obs_server.wait_for_client_count(0)
    assert LOST in events.names() and events.count(CONNECTED) == 1


async def test_close_while_a_reconnect_is_opening_leaves_nothing_open(obs_server, make_gateway, monkeypatch):
    gateway, events = make_gateway()
    await gateway.connect()
    entered, release, opened = threading.Event(), threading.Event(), threading.Event()
    real_open = client_module._Link.open

    def slow_open(link, *args):
        entered.set()
        release.wait(5.0)
        real_open(link, *args)
        opened.set()

    monkeypatch.setattr(client_module._Link, "open", slow_open)
    await obs_server.drop_clients()
    await wait_until(entered.is_set)

    closing = asyncio.create_task(gateway.close())
    await asyncio.sleep(0.05)
    release.set()
    await closing
    await wait_until(opened.is_set)

    await obs_server.wait_for_client_count(0)
    assert events.names() == [CONNECTED, LOST]
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd /home/light/Projects/anki_miner_game/.worktrees/t12-obs-gateway && ./.venv/bin/pytest tests/obs/test_client.py -n 8 -p no:cacheprovider -q`
(`-n 8`: the failing tests each wait out a 5 s `wait_until`.)
Expected: `9 failed, 32 passed`. The failures are
`test_the_credentials_are_read_again_at_every_connect`,
`test_requests_raise_obs_auth_error_while_the_password_is_refused`,
`test_a_request_that_times_out_drops_the_connection_and_reconnects`,
`test_a_dropped_connection_is_announced_and_reconnected[vanished]` and `[obs-exit]`,
`test_reconnect_backs_off_1_2_5_10_then_10_seconds`, `test_requests_fail_fast_while_reconnecting`,
`test_close_during_reconnect_attempts_leaves_nothing_open` and
`test_close_while_a_reconnect_is_opening_leaves_nothing_open`, each on a `TimeoutError` from
`wait_until` or `wait_for_client_count`. The two new tests that already pass are guards:
`test_a_failed_first_connect_starts_no_reconnecting` (decision 3) and
`test_connect_during_the_backoff_connects_at_once` (the loop must not add a second `_Connected`).

- [ ] **Step 3: Add reconnecting to the gateway**

Edit `anki_miner_game/obs/client.py`:

1. Add `ObsError` to the `anki_miner_game.models.obs` import, between `ObsCredentials` and
   `ObsEventName`.

2. Below `NOT_READY: Final = 207` add

```python
BACKOFF_S: Final = (1.0, 2.0, 5.0, 10.0)
"""Waits before successive reconnect attempts; the last one repeats (the text sources' schedule)."""
```

3. In `ObsClient.__init__`, below `self._connect_lock = asyncio.Lock()`, add

```python
        self._reconnect_task: asyncio.Task[None] | None = None
```

4. In the `connect` docstring, replace `the timeout).` with
   `the timeout). A failed ``connect`` starts no reconnecting.`

5. Replace the start of `close`

```python
    async def close(self) -> None:
        """Disconnect and stop reconnecting; no ``_ConnectionLost`` follows. ``connect`` may be called again."""
        self._closed = True
```

with

```python
    async def close(self) -> None:
        """Disconnect and stop reconnecting; no ``_ConnectionLost`` follows. ``connect`` may be called again."""
        self._closed = True
        task, self._reconnect_task = self._reconnect_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
```

6. Replace `_open`

```python
    async def _open(self) -> "_Link":
        credentials = await self._run(self._credentials)
        link = _Link()
        await self._run(link.open, credentials, partial(self._on_event, link), partial(self._on_end, link))
        return link
```

with

```python
    async def _open(self) -> "_Link":
        credentials = await self._run(self._credentials)
        link = _Link()
        opening = self._run(link.open, credentials, partial(self._on_event, link), partial(self._on_end, link))
        try:
            await asyncio.shield(opening)
        except asyncio.CancelledError:  # close() during a reconnect: the worker still finishes opening
            with contextlib.suppress(Exception):
                await opening
            await self._teardown(link)
            raise
        return link
```

   `close()` cancels a reconnect that may be inside `link.open` on the worker thread; the worker
   finishes opening regardless, so the cancelled attempt waits for it and tears the link down.
   `test_close_while_a_reconnect_is_opening_leaves_nothing_open` pins this: without the shield it
   times out with the two late connections still open (checked while planning).

7. Replace the loss section

```python
    # after a loss (on the loop)

    def _after_loss(self, link: "_Link") -> None:
        if self._link is link:
            self._link = None
        self._spawn(self._teardown(link))
```

with

```python
    # reconnecting (on the loop)

    def _after_loss(self, link: "_Link") -> None:
        if self._link is link:
            self._link = None
        self._spawn(self._teardown(link))
        if not self._closed and (self._reconnect_task is None or self._reconnect_task.done()):
            self._reconnect_task = asyncio.get_running_loop().create_task(self._reconnect())

    async def _reconnect(self) -> None:
        attempt = 0
        while True:
            await self._sleep(BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)])
            attempt += 1
            async with self._connect_lock:
                if self._closed or (self._link is not None and not self._link.lost):
                    return
                try:
                    await self._connect_once()
                except ObsError as exc:
                    logger.info("reconnecting to OBS failed: %s", exc)
                    continue
                except Exception:
                    logger.exception("reconnecting to OBS failed")
                    continue
            return
```

- [ ] **Step 4: Run the tests to see them pass**

Run: `cd /home/light/Projects/anki_miner_game/.worktrees/t12-obs-gateway && ./.venv/bin/pytest tests/obs/test_client.py tests/fakes/test_fake_obs_server.py -n0 -p no:cacheprovider -q`
Expected: `54 passed` (41 + 13) in about 3 s.

- [ ] **Step 5: Check for flakiness**

Run the gateway tests ten times in parallel:

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t12-obs-gateway && for i in 1 2 3 4 5 6 7 8 9 10; do ./.venv/bin/pytest tests/obs/test_client.py -n 16 --dist load -p no:cacheprovider -q 2>&1 | tail -1; done
```

Expected: ten lines of `41 passed`. Any failure is a race in the code or the test: fix the cause
(the test waits on a state the gateway has not reached yet, as `wait_until(... backoffs() >= 2)`
does in the auth test), never add sleeps or retries.

- [ ] **Step 6: Type-check, format, lint, commit**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t12-obs-gateway && ./.venv/bin/mypy anki_miner_game && ./.venv/bin/black anki_miner_game/obs/client.py tests/obs/test_client.py && ./.venv/bin/ruff check anki_miner_game/obs/client.py tests/obs/test_client.py
git -C /home/light/Projects/anki_miner_game/.worktrees/t12-obs-gateway add anki_miner_game/obs/client.py tests/obs/test_client.py
git -C /home/light/Projects/anki_miner_game/.worktrees/t12-obs-gateway commit -m "feat(obs): reconnect the gateway with backoff after a lost connection"
```

---

## Task 5: the recorded sessions through the gateway; settle the provisional constants

**Files:**
- Create: `tests/obs/test_client_transcripts.py`
- Modify: `anki_miner_game/obs/client.py` (the provisional block only, Step 5)

The card's "one test per transcript". `replay()` sends every recorded request through the
gateway once its generation is connected (a 207 run is one request: the gateway retries it),
compares each answer with the recording (`responseData`, or code and comment for a failure),
leaves never-answered requests pending until `close()` (they must end as `ObsConnectError`), and
then compares everything the handlers heard with `Transcript.expected_events(67, ...)`: the
subscribed events with their recorded `eventData`, `_Connected` per generation and
`_ConnectionLost` where the recording drops or loses a connection. The sample test pins
`expected_events` itself against a hand-written list. These tests exercise code Tasks 1-4
already built, so they are expected to pass on the first run; a failure is a finding, and
"Which tests need which transcript" above says how to handle the two known shapes the loader does
not take.

- [ ] **Step 1: Check the R2 transcripts are on the branch**

Run: `ls /home/light/Projects/anki_miner_game/.worktrees/t12-obs-gateway/tests/fixtures/obs_transcripts/*.jsonl | wc -l`
Expected: the number of transcripts the M0 gate committed (24 in R2's draft set). If it prints 0,
merge `main` (Working context); if `main` has none either, write Steps 2-4 anyway, commit them,
set the status file's `stage` to `blocked-on-r2` with the head sha, and stop: the gate cannot
pass while `test_the_recorded_transcripts_are_present` fails.

- [ ] **Step 2: Write the tests**

`tests/obs/test_client_transcripts.py`:

```python
"""The OBS gateway replaying sessions recorded from a real OBS (spec 18.2; M0 spike R2).

One test per transcript in ``tests/fixtures/obs_transcripts/``: the test sends each recorded request
through the gateway once its connection is up, checks the answer against the recording, and checks
that the handlers saw ``_Connected``, the recorded events the gateway subscribes to, and
``_ConnectionLost`` exactly where the recording has them.
"""

import asyncio
from pathlib import Path

import pytest

from anki_miner_game.models.obs import ObsConnectError, ObsRequestError
from anki_miner_game.obs.client import EVENT_SUBSCRIPTIONS, ObsClient
from tests.fakes.fake_obs_server import FakeObsServer, Transcript, load_transcript
from tests.fakes.obs_transcript_sample import RECORDING_PATH, write_sample_transcript
from tests.obs.helpers import CONNECTED, LOST, Credentials, Events, FakeClock, wait_until

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "obs_transcripts"
TRANSCRIPTS = sorted(FIXTURES.glob("*.jsonl"))
REPLAY_TIMEOUT_S = 20.0


async def replay(transcript: Transcript) -> tuple[Events, FakeObsServer]:
    """Drive a gateway through ``transcript`` against a replaying FakeObsServer; return what it heard."""
    async with FakeObsServer(transcript=transcript) as server:
        clock = FakeClock()
        gateway = ObsClient(Credentials(server, None), now=clock.now, sleep=clock.sleep)
        events = Events()
        gateway.subscribe(events)
        unanswered: list[asyncio.Task[dict]] = []
        try:
            async with asyncio.timeout(REPLAY_TIMEOUT_S):
                await gateway.connect()
                for exchange in transcript.exchanges():
                    await wait_until(lambda g=exchange.generation: events.count(CONNECTED) > g, REPLAY_TIMEOUT_S)
                    sent = gateway.request(exchange.request_type, **exchange.request_data)
                    if exchange.response is None:
                        unanswered.append(asyncio.create_task(sent))
                    elif exchange.response["requestStatus"]["result"]:
                        assert await sent == dict(exchange.response.get("responseData") or {}), exchange
                    else:
                        with pytest.raises(ObsRequestError) as raised:
                            await sent
                        status = exchange.response["requestStatus"]
                        assert (raised.value.code, raised.value.comment) == (status["code"], status.get("comment", ""))
                await server.replay_finished.wait()
                expected = transcript.expected_events(EVENT_SUBSCRIPTIONS, CONNECTED, LOST)
                await wait_until(lambda: len(events.got) >= len(expected), REPLAY_TIMEOUT_S)
        except TimeoutError:
            pytest.fail(f"replay stuck at {server.replay_position}; heard {events.names()}")
        finally:
            await gateway.close()
        for outcome in await asyncio.gather(*unanswered, return_exceptions=True):
            assert isinstance(outcome, ObsConnectError), outcome
        assert server.unscripted == [] and server.errors == []
        return events, server


async def test_the_gateway_replays_the_sample_transcript(tmp_path):
    transcript = load_transcript(write_sample_transcript(tmp_path))

    events, server = await replay(transcript)

    assert [(e.name, dict(e.data)) for e in events.got] == [
        (CONNECTED, {}),
        ("CurrentProfileChanged", {"profileName": "Anki Miner Game"}),
        (
            "RecordStateChanged",
            {"outputActive": False, "outputPath": None, "outputState": "OBS_WEBSOCKET_OUTPUT_STARTING"},
        ),
        (
            "RecordStateChanged",
            {"outputActive": True, "outputPath": RECORDING_PATH, "outputState": "OBS_WEBSOCKET_OUTPUT_STARTED"},
        ),
        ("CurrentSceneCollectionChanging", {"sceneCollectionName": "Untitled"}),
        ("CurrentSceneCollectionChanged", {"sceneCollectionName": "Anki Miner Game"}),
        (LOST, {}),
        (CONNECTED, {}),
        ("ExitStarted", {}),
        (LOST, {}),
    ]
    assert server.request_types().count("GetStreamStatus") == 3
    assert server.refuse_connections


def test_the_recorded_transcripts_are_present():
    assert TRANSCRIPTS, f"no R2 transcripts in {FIXTURES}: merge main after the M0 gate"


@pytest.mark.parametrize("path", TRANSCRIPTS, ids=[path.stem for path in TRANSCRIPTS])
async def test_the_gateway_replays_a_recorded_obs_session(path):
    transcript = load_transcript(path)

    events, _ = await replay(transcript)

    assert [(e.name, dict(e.data)) for e in events.got] == transcript.expected_events(
        EVENT_SUBSCRIPTIONS, CONNECTED, LOST
    )
```

- [ ] **Step 3: Run them**

Run: `cd /home/light/Projects/anki_miner_game/.worktrees/t12-obs-gateway && ./.venv/bin/pytest tests/obs/test_client_transcripts.py -n 4 -p no:cacheprovider -q`
Expected: `N+2 passed` for N transcripts, in a few seconds (the replay runs on the fake clock;
reconnects are instant).

- [ ] **Step 4: Commit**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t12-obs-gateway && ./.venv/bin/black tests/obs/test_client_transcripts.py && ./.venv/bin/ruff check tests/obs/test_client_transcripts.py
git -C /home/light/Projects/anki_miner_game/.worktrees/t12-obs-gateway add tests/obs/test_client_transcripts.py
git -C /home/light/Projects/anki_miner_game/.worktrees/t12-obs-gateway commit -m "test(obs): replay every recorded OBS session through the gateway"
```

- [ ] **Step 5: Settle the provisional constants from R2**

Read `docs/m0/obs-behaviour.md` and the spec amendments the M0 gate made to 3.3 and 11.2, and
find three measurements: (a) the time from OBS's start until `GetVersion` succeeds, (b) the
longest `CurrentSceneCollectionChanging` to `CurrentSceneCollectionChanged` interval, (c) the
longest time a blocking request (`SetCurrentProfile`, `SetCurrentSceneCollection`,
`CreateSceneCollection`, `StopRecord`) took to be answered, leaving out the answer held back by
the restart-prompt modal. Grep each number you use in the transcript it cites before relying on
it. Then:

- `NOT_READY_TIMEOUT_S` = the larger of 30 and twice the larger of (a) and (b), rounded up to a
  multiple of 5.
- `REQUEST_TIMEOUT_S` = the larger of 20 and twice (c), rounded up to a multiple of 5. It stays
  above the actor's 15 s switch timeout (spec 6.2 step 3), so a switch the actor gives up on is
  reported by the actor, not by a dropped connection.
- `NOT_READY_RETRY_S` stays 0.25 unless the document recommends a retry interval.
- Replace the comment above the three constants with one naming the source, for example
  `# From R2 (docs/m0/obs-behaviour.md, "Scene collection switch"): OBS loaded in 4.1 s, the longest switch took 0.9 s, the slowest blocking answer 0.6 s. Linux measurements; Windows provisional until H5 (D2).`
  If the document has none of the three numbers, keep the values and the "Provisional until R2"
  comment, and say so in your report.

Then run the gateway tests (the tests read the constants, none hard-codes 30, 0.25 or 20):

Run: `cd /home/light/Projects/anki_miner_game/.worktrees/t12-obs-gateway && ./.venv/bin/pytest tests/obs -n 4 -p no:cacheprovider -q`
Expected: all pass.

If a value changed:

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t12-obs-gateway && ./.venv/bin/black anki_miner_game/obs/client.py && ./.venv/bin/ruff check anki_miner_game/obs/client.py
git -C /home/light/Projects/anki_miner_game/.worktrees/t12-obs-gateway add anki_miner_game/obs/client.py
git -C /home/light/Projects/anki_miner_game/.worktrees/t12-obs-gateway commit -m "chore(obs): set the gateway's OBS timeouts from R2's measurements"
```

If only the comment changed, use the same commit with the subject
`docs(obs): cite R2 for the gateway's OBS timeouts`.

---

## Task 6: gate and status

- [ ] **Step 1: Run the gate**

```bash
cd /home/light/Projects/anki_miner_game/.worktrees/t12-obs-gateway && PYTEST_XDIST_AUTO_NUM_WORKERS=4 bash scripts/health.sh > /home/light/Projects/anki_miner_game/.worktrees/t12-obs-gateway/gate.log 2>&1; echo "gate exit $?"
grep -E '^(PASS|FAIL) |SUMMARY' /home/light/Projects/anki_miner_game/.worktrees/t12-obs-gateway/gate.log
```

Expected: `gate exit 0`; `PASS black`, `PASS ruff`, `PASS mypy`, `PASS pytest`. `gate.log` is
gitignored; keep it as the evidence. Never pipe the gate through `tail`.

- [ ] **Step 2: Update the status file**

Write `/home/light/Projects/anki_miner_game/.orchestration/status/t12-obs-gateway.json` keeping
`slug`, `base_sha`, `branch`, `worktree` and setting `"stage": "implemented"`,
`"head_sha": "<git rev-parse HEAD>"`, `"gate_exit": 0`, `"gate_summary": "<the SUMMARY lines>"`,
`"contract_change_request": ""`.

- [ ] **Step 3: Report**

Return the implementer schema. In `notes`, tell the orchestrator:
- For T15: `request()` raises `ObsAuthError` (not only `ObsConnectError`) while the reconnect loop
  is failing on the password, so the actor raises the spec 17 password banner from either
  `connect()` or `request()`; `connect()` during the backoff tries at once; `close()` sends no
  `_ConnectionLost` and the gateway can `connect()` again after it; events that arrive before
  `_Connected` are delivered right after it with their arrival stamps.
- For T16: `ObsClient(lambda: discovery.credentials(<current AppConfig>), now=time.monotonic)`;
  the credentials callable runs on the gateway's worker thread.
- For T14 and T25: `FakeObsServer(transcript=load_transcript(path))` binds clients by role, drops
  other clients' connections and answers `GetVersion` live (decision 13);
  `Transcript.exchanges()` and `expected_events()` drive a replay as
  `tests/obs/test_client_transcripts.py::replay` does.
- For H5 and T30: websocket-client honours `http_proxy` for `ws://127.0.0.1` (decision 15).
- The values Task 5 settled, or that they stay provisional.
