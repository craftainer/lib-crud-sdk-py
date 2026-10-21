"""SSE subscribers that turn `GET {base_path}/events` into cache invalidation.

One connection per resource, reference-counted and fanned out to every listener. The caller
injects an already authenticated ``httpx2.Client``/``AsyncClient``. Frames carry no ``event:``
name: the first has ``id: 0`` and ``{"subscriber_id": ...}``; later ones are record events.
A reconnect passes ``?{resume_param}=<subscriber_id>`` explicitly (never ``Last-Event-ID``, which
the server would read as a subscriber id) when the stream replays, and always tells listeners to
reset, so the gap is covered even without replay. A stream silent for three keep-alive
intervals is dead (the read timeout) and is reconnected with exponential backoff and jitter.
Events invalidate; they never apply snapshots.
"""

import asyncio
import logging
import random
import threading
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal, cast, overload

import httpx2

from craftainer_crud_sdk.cache.layer import AsyncCacheLayer, CacheLayer
from craftainer_crud_sdk.interfaces import CrudEvent

__all__ = [
    "AsyncEventSubscriber",
    "EventSubscriber",
    "invalidate_on_events",
]

log = logging.getLogger(__name__)

BACKOFF_START = 1.0
BACKOFF_MAX = 30.0
DEAD_AFTER_KEEPALIVES = 3
_ACTIONS = frozenset({"create", "update", "delete", "restore"})

type _Action = Literal["create", "update", "delete", "restore"]


@dataclass(frozen=True, slots=True)
class _Handshake:
    subscriber_id: str


@dataclass(frozen=True, slots=True)
class _Reset:
    """An event that cannot be mapped to one record: listeners clear the resource."""


type _Frame = _Handshake | _Reset | CrudEvent | None


class _Stream:
    """Protocol state shared by the sync and async subscribers (no I/O)."""

    def __init__(
        self,
        events: Mapping[str, object],
        url: str | None,
        jitter: Callable[[], float],
    ) -> None:
        path = events.get("path")
        self.url = url if url is not None else str(path)
        self.label = events.get("resource")
        self.resume_param = str(events.get("resume_param") or "subscriber_id")
        self.persistent = events.get("replay") == "persistent"
        keepalive = events.get("keepalive_seconds")
        self.keepalive = float(keepalive) if isinstance(keepalive, int | float) else 15.0
        self.jitter = jitter
        self.subscriber_id: str | None = None
        self.connections = 0
        self.attempt = 0

    @property
    def params(self) -> dict[str, str] | None:
        if self.persistent and self.subscriber_id is not None:
            return {self.resume_param: self.subscriber_id}
        return None

    @property
    def timeout(self) -> httpx2.Timeout:
        """Read timeout of three keep-alive intervals: silence longer than that is a dead stream."""
        return httpx2.Timeout(10.0, read=self.keepalive * DEAD_AFTER_KEEPALIVES)

    def backoff(self) -> float:
        delay = min(BACKOFF_START * 2**self.attempt, BACKOFF_MAX)
        self.attempt += 1
        return float(delay * (0.5 + 0.5 * self.jitter()))

    def decode(self, sse: httpx2.ServerSentEvent) -> _Frame:
        try:
            data = sse.json()
        except ValueError:
            log.warning("ignoring a non-JSON event frame")
            return None
        if not isinstance(data, dict):
            return None
        if "subscriber_id" in data:
            return _Handshake(str(data["subscriber_id"]))
        if data.get("resource") != self.label:
            return None
        action = data.get("action")
        record_id = data.get("record_id")
        if action not in _ACTIONS or not isinstance(record_id, int) or isinstance(record_id, bool):
            return _Reset()
        snapshot = data.get("snapshot")
        return CrudEvent(
            resource=str(data["resource"]),
            record_id=record_id,
            action=cast("_Action", action),
            snapshot=snapshot if isinstance(snapshot, dict) else {},
            timestamp=str(data.get("timestamp", "")),
        )

    def handshake(self, frame: _Handshake) -> bool:
        """Record the handshake; return whether this is a reconnect (listeners must reset)."""
        reconnect = self.connections > 0
        self.connections += 1
        self.subscriber_id = frame.subscriber_id
        self.attempt = 0
        return reconnect


def _default_jitter() -> Callable[[], float]:
    return random.SystemRandom().random


@dataclass(slots=True)
class _Listener:
    on_event: Callable[[CrudEvent], None]
    on_reset: Callable[[], None] | None
    on_status: Callable[[bool], None] | None


class EventSubscriber:
    """Synchronous SSE subscriber: a daemon thread per resource, started by the first listener."""

    def __init__(
        self,
        client: httpx2.Client,
        events: Mapping[str, object],
        url: str | None = None,
        *,
        jitter: Callable[[], float] | None = None,
    ) -> None:
        """``events`` is the manifest ``capabilities.events`` dict; ``url`` defaults to its path."""
        self._client = client
        self._stream = _Stream(events, url, jitter or _default_jitter())
        self._lock = threading.Lock()
        self._listeners: list[_Listener] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._connected = False
        self._response: httpx2.Response | None = None  # the live stream, closed to unblock it

    def subscribe(
        self,
        on_event: Callable[[CrudEvent], None],
        *,
        on_reset: Callable[[], None] | None = None,
        on_status: Callable[[bool], None] | None = None,
    ) -> Callable[[], None]:
        """Add a listener; returns its unsubscribe function. The last one stops the stream.

        ``on_reset`` fires after a reconnect (and for an event with no usable record id),
        ``on_status(True/False)`` when the stream becomes connected or is lost.
        """
        listener = _Listener(on_event, on_reset, on_status)
        with self._lock:
            self._listeners.append(listener)
            # A reader that is stopping (its stop event is set) is no stream for this listener:
            # start a new generation with its own stop event; the old one only winds down.
            start = self._thread is None or self._stop.is_set()
            if start:
                self._stop = threading.Event()
                self._connected = False
                self._response = None
                self._thread = threading.Thread(target=self.run, args=(self._stop,), daemon=True)
            connected = self._connected
        if connected and on_status is not None:
            on_status(True)
        if start:
            cast("threading.Thread", self._thread).start()
        return lambda: self._unsubscribe(listener)

    def _unsubscribe(self, listener: _Listener) -> None:
        with self._lock:
            if listener not in self._listeners:
                return
            self._listeners.remove(listener)
            last = not self._listeners
            if last:  # decided under the lock, so a racing subscribe sees a stopping reader
                self._stop.set()
        if listener.on_status is not None and self._connected:
            listener.on_status(False)
        if last:
            self._stop_reader()

    def _stop_reader(self) -> None:
        """Ask the loop to stop and unblock a reader waiting for a frame by closing its stream."""
        with self._lock:
            self._stop.set()
            response = self._response
        if response is not None:
            response.close()

    def close(self, timeout: float = 5.0) -> None:
        """Stop the stream, unblock the reader and wait up to ``timeout`` seconds for its thread.

        After it returns the thread has exited (a warning is logged if it has not), so the
        HTTP client may be closed safely.
        """
        self._stop_reader()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
            if thread.is_alive():
                log.warning("event stream %s did not stop within %ss", self._stream.url, timeout)

    def run(self, stop: threading.Event | None = None) -> None:
        """Connect, read and reconnect until stopped (blocking; the thread's body).

        ``stop`` is this reader's own stop event (the current one by default): a reader that was
        replaced by a newer generation never touches the newer one's state. However it ends,
        listeners get ``on_status(False)`` and the thread state is reset, so a later `subscribe`
        starts a new stream.
        """
        stop = stop or self._stop
        stream = self._stream
        try:
            while not stop.is_set():
                try:
                    self._read_once(stop)
                except Exception as exc:
                    if stop.is_set():
                        break  # the stream was closed under the reader
                    if not isinstance(exc, httpx2.HTTPError):
                        raise
                    log.warning("event stream %s lost: %s", stream.url, exc)
                self._set_connected(False, stop)
                if stop.is_set():
                    break
                stop.wait(stream.backoff())
        except Exception:
            log.exception("event stream %s died", stream.url)
        finally:
            self._set_connected(False, stop)
            with self._lock:
                if self._stop is stop:  # not replaced by a newer reader
                    self._thread = None

    def _read_once(self, stop: threading.Event) -> None:
        stream = self._stream
        with self._client.sse(stream.url, params=stream.params, timeout=stream.timeout) as source:
            with self._lock:  # a stopped reader (possibly replaced already) publishes nothing
                stopped = stop.is_set()
                if not stopped:
                    self._response = source.response
            if stopped:
                return  # stopped before the response was published: nothing would close it
            try:
                self._read_frames(source, stop)
            finally:
                with self._lock:
                    if self._response is source.response:
                        self._response = None

    def _read_frames(self, source: httpx2.EventSource, stop: threading.Event) -> None:
        stream = self._stream
        source.response.raise_for_status()
        for sse in source:
            if stop.is_set():
                return
            frame = stream.decode(sse)
            if isinstance(frame, _Handshake):
                if stream.handshake(frame):
                    self._fan_reset()
                self._set_connected(True, stop)
            elif isinstance(frame, _Reset):
                self._fan_reset()
            elif frame is not None:
                self._fan_event(frame)

    def _snapshot(self) -> list[_Listener]:
        with self._lock:
            return list(self._listeners)

    def _set_connected(self, connected: bool, stop: threading.Event) -> None:
        with self._lock:
            if self._stop is not stop or self._connected == connected:
                return  # a replaced reader must not flip the newer reader's status
            self._connected = connected
            listeners = list(self._listeners)
        for listener in listeners:
            if listener.on_status is not None:
                _guard(listener.on_status, connected)

    def _fan_reset(self) -> None:
        for listener in self._snapshot():
            if listener.on_reset is not None:
                _guard(listener.on_reset)

    def _fan_event(self, event: CrudEvent) -> None:
        for listener in self._snapshot():
            _guard(listener.on_event, event)


def _guard[**P](fn: Callable[P, None], *args: P.args, **kwargs: P.kwargs) -> None:
    """Run a listener; one that raises must not kill the stream."""
    try:
        fn(*args, **kwargs)
    except Exception:
        log.exception("event listener failed")


@dataclass(slots=True)
class _AsyncListener:
    on_event: Callable[[CrudEvent], Awaitable[None]]
    on_reset: Callable[[], Awaitable[None]] | None
    on_status: Callable[[bool], Awaitable[None]] | None


@dataclass(slots=True)
class _AsyncState:
    listeners: list[_AsyncListener] = field(default_factory=list)
    task: asyncio.Task[None] | None = None
    connected: bool = False
    tasks: set[asyncio.Task[None]] = field(default_factory=set)  # every live reader and notifier


class AsyncEventSubscriber:
    """Asynchronous SSE subscriber: one task per resource, started by the first listener."""

    def __init__(
        self,
        client: httpx2.AsyncClient,
        events: Mapping[str, object],
        url: str | None = None,
        *,
        jitter: Callable[[], float] | None = None,
    ) -> None:
        """``events`` is the manifest ``capabilities.events`` dict; ``url`` defaults to its path."""
        self._client = client
        self._stream = _Stream(events, url, jitter or _default_jitter())
        self._state = _AsyncState()

    def subscribe(
        self,
        on_event: Callable[[CrudEvent], Awaitable[None]],
        *,
        on_reset: Callable[[], Awaitable[None]] | None = None,
        on_status: Callable[[bool], Awaitable[None]] | None = None,
    ) -> Callable[[], None]:
        """Add a listener (call inside a running loop); returns its unsubscribe function."""
        listener = _AsyncListener(on_event, on_reset, on_status)
        state = self._state
        state.listeners.append(listener)
        loop = asyncio.get_running_loop()
        if state.task is None:
            state.connected = False
            state.task = self._track(loop.create_task(self.run()))
        elif state.connected and on_status is not None:
            self._track(loop.create_task(self._notify_connected(listener)))
        return lambda: self._unsubscribe(listener)

    def _track(self, task: asyncio.Task[None]) -> asyncio.Task[None]:
        self._state.tasks.add(task)
        task.add_done_callback(self._state.tasks.discard)
        return task

    async def _notify_connected(self, listener: _AsyncListener) -> None:
        """Tell a listener that joined a connected stream (unless that changed meanwhile)."""
        state = self._state
        if listener in state.listeners and state.connected and listener.on_status is not None:
            await _aguard(listener.on_status(True))

    def _unsubscribe(self, listener: _AsyncListener) -> None:
        state = self._state
        if listener not in state.listeners:
            return
        state.listeners.remove(listener)
        if not state.listeners and state.task is not None:
            # Detach at once: a subscribe before the cancelled task has exited starts a new reader.
            state.task.cancel()
            state.task = None
            state.connected = False

    async def aclose(self) -> None:
        """Stop the stream and wait for its task."""
        tasks = list(self._state.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def run(self) -> None:
        """Connect, read and reconnect until cancelled."""
        stream = self._stream
        try:
            while True:
                try:
                    await self._read_once()
                except httpx2.HTTPError as exc:
                    log.warning("event stream %s lost: %s", stream.url, exc)
                await self._set_connected(False)
                await asyncio.sleep(stream.backoff())
        except Exception:
            log.exception("event stream %s died", stream.url)
        finally:
            await self._finished()

    async def _finished(self) -> None:
        """On any exit (cancel, close, a bug) tell the listeners, so the gate closes.

        Not when the task was detached (the last listener left) or replaced by a newer reader:
        that one's state is not ours to reset.
        """
        state = self._state
        if state.task is not asyncio.current_task():
            return
        state.task = None
        await self._set_connected(False)

    async def _read_once(self) -> None:
        stream = self._stream
        async with self._client.sse(
            stream.url, params=stream.params, timeout=stream.timeout
        ) as source:
            source.response.raise_for_status()
            async for sse in source:
                frame = stream.decode(sse)
                if isinstance(frame, _Handshake):
                    if stream.handshake(frame):
                        await self._fan_reset()
                    await self._set_connected(True)
                elif isinstance(frame, _Reset):
                    await self._fan_reset()
                elif frame is not None:
                    for listener in list(self._state.listeners):
                        await _aguard(listener.on_event(frame))

    async def _set_connected(self, connected: bool) -> None:
        state = self._state
        if state.connected == connected:
            return
        state.connected = connected
        for listener in list(state.listeners):
            if listener.on_status is not None:
                await _aguard(listener.on_status(connected))

    async def _fan_reset(self) -> None:
        for listener in list(self._state.listeners):
            if listener.on_reset is not None:
                await _aguard(listener.on_reset())


async def _aguard(awaitable: Awaitable[None]) -> None:
    try:
        await awaitable
    except Exception:
        log.exception("event listener failed")


@overload
def invalidate_on_events(
    cache_layer: CacheLayer, subscriber: EventSubscriber, resource_name: str
) -> Callable[[], None]: ...
@overload
def invalidate_on_events(
    cache_layer: AsyncCacheLayer, subscriber: AsyncEventSubscriber, resource_name: str
) -> Callable[[], None]: ...
def invalidate_on_events(
    cache_layer: CacheLayer | AsyncCacheLayer,
    subscriber: EventSubscriber | AsyncEventSubscriber,
    resource_name: str,
) -> Callable[[], None]:
    """Wire a subscriber to a cache layer for the manifest resource ``resource_name``.

    Events clear the spec's prefixes, a reconnect clears the resource, and fresh hits are allowed
    only while the stream is connected. Returns the unsubscribe function (it also closes the
    freshness gate).
    """
    if isinstance(cache_layer, CacheLayer) and isinstance(subscriber, EventSubscriber):
        sync_layer = cache_layer

        def on_event(event: CrudEvent) -> None:
            sync_layer.invalidate_event(resource_name, event.action, event.record_id)

        def on_reset() -> None:
            sync_layer.clear_resource(resource_name)

        def on_status(connected: bool) -> None:
            sync_layer.set_events_active(resource_name, connected)

        unsubscribe = subscriber.subscribe(on_event, on_reset=on_reset, on_status=on_status)
    else:
        async_layer = cast("AsyncCacheLayer", cache_layer)

        async def aon_event(event: CrudEvent) -> None:
            await async_layer.invalidate_event(resource_name, event.action, event.record_id)

        async def aon_reset() -> None:
            await async_layer.clear_resource(resource_name)

        async def aon_status(connected: bool) -> None:
            async_layer.set_events_active(resource_name, connected)

        unsubscribe = cast("AsyncEventSubscriber", subscriber).subscribe(
            aon_event, on_reset=aon_reset, on_status=aon_status
        )

    def stop() -> None:
        unsubscribe()
        cache_layer.set_events_active(resource_name, False)

    return stop
