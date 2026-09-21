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
- websocket-client (under obsws-python) keeps a socket open when its ``close()`` runs after the
  server's close frame, so this module never relies on it: it shuts both sockets down itself on
  ``close()`` and after a lost connection.
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
import socket
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from typing import Any, Final, NoReturn, TypeVar

import obsws_python as obsws
from obsws_python.error import OBSSDKError, OBSSDKTimeoutError

from anki_miner_game.models.messages import ObsEvent
from anki_miner_game.models.obs import (
    ObsAuthError,
    ObsConnectError,
    ObsCredentials,
    ObsError,
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
BACKOFF_S: Final = (1.0, 2.0, 5.0, 10.0)
"""Waits before successive reconnect attempts; the last one repeats (the text sources' schedule)."""
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
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._connect_lock = asyncio.Lock()
        self._reconnect_task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    # ObsGateway

    async def connect(self) -> ObsInfo:
        """Connect unless connected; see ``ObsGateway.connect``.

        Raises ``ObsConnectError`` (OBS unreachable, or ``close()`` ran before the connection was
        up; nothing stays open), ``ObsAuthError`` (the password was refused
        twice, the credentials read again in between), ``ObsConfigError`` (from ``credentials``),
        ``ObsUnsupportedError`` (a required request is missing) or ``ObsRequestError`` (207 past
        the timeout). A failed ``connect`` starts no reconnecting.
        """
        self._loop = asyncio.get_running_loop()
        self._closed = False
        async with self._connect_lock:
            if self._link is not None and not self._link.lost and self._info is not None:
                return self._info
            return await self._connect_once()

    async def request(self, name: str, **fields: Any) -> dict[str, Any]:
        """See ``ObsGateway.request``."""
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
        task, self._reconnect_task = self._reconnect_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
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
            raise ObsAuthError("OBS refused the websocket password") from None

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
            if self._closed:
                raise ObsConnectError("the gateway was closed while it was connecting")
            link.ready = True
            self._link = link
            self._deliver(ObsEvent(ObsEventName.CONNECTED, {}, self._now()))
            for event in link.held:
                self._deliver(event)
            link.held.clear()

    # requests

    def _require_link(self) -> "_Link":
        link = self._link
        if link is None or link.lost:
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
            if self._closed:  # a connect() still retrying while OBS loads keeps nothing open past close()
                raise ObsConnectError(f"the gateway was closed while OBS was not ready for {name}")

    async def _send(self, link: "_Link", name: str, fields: Mapping[str, Any]) -> dict[str, Any]:
        if link.lost or link.closed:
            raise ObsConnectError("not connected to OBS")
        job = self._submit(_blocking_send, link.req, name, dict(fields))
        try:
            return await asyncio.wrap_future(job)
        except asyncio.CancelledError:
            if not job.cancel():  # already sent: OBS's answer would be read as the next request's
                self._lose(link, f"{name} was cancelled before OBS answered")
            raise
        except OBSSDKTimeoutError as exc:  # e.g. held back by a modal dialog in OBS (R2 item 3)
            self._lose(link, f"{name}: no answer within {REQUEST_TIMEOUT_S:g} s")
            raise ObsConnectError(
                f"OBS did not answer {name} within {REQUEST_TIMEOUT_S:g} s; dropped the connection"
            ) from exc
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

    async def _teardown(self, link: "_Link") -> None:
        link.abort()  # wakes a request blocked on this connection
        with contextlib.suppress(Exception):
            await self._run(link.shutdown)

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _submit(self, fn: Callable[..., T], *args: Any) -> "Future[T]":
        """Queue blocking obsws-python work on the gateway's one worker thread."""
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="obs-gateway")
        return self._executor.submit(fn, *args)

    def _run(self, fn: Callable[..., T], *args: Any) -> "asyncio.Future[T]":
        """``_submit``, awaitable from the loop (what ``run_in_executor`` does)."""
        return asyncio.wrap_future(self._submit(fn, *args))


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
        """Any thread: wake whatever is blocked reading either socket.

        Shuts the sockets down directly: websocket-client's ``abort()`` does nothing once the
        server's close frame has arrived, while the event thread may still be reading.
        """
        for client in (self.req, self.evt):
            sock = getattr(getattr(getattr(client, "base_client", None), "ws", None), "sock", None)
            if sock is not None:
                with contextlib.suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)

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
