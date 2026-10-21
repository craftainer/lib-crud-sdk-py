"""SSE subscribers and the invalidation wiring."""

import asyncio
import json
import logging
import threading
from collections.abc import AsyncIterator, Callable, Iterator

import httpx2
import pytest

from craftainer_crud_sdk.cache import (
    AsyncCacheLayer,
    AsyncMemoryStore,
    CacheLayer,
    CachePolicy,
    MemoryStore,
    build_key,
)
from craftainer_crud_sdk.events import (
    AsyncEventSubscriber,
    EventSubscriber,
    _Listener,
    invalidate_on_events,
)
from craftainer_crud_sdk.interfaces import CacheEntry, CrudEvent

EVENTS = {
    "transport": "sse",
    "path": "/api/heroes/events",
    "resource": "hero",
    "resume_param": "subscriber_id",
    "replay": "persistent",
    "keepalive_seconds": 10,
    "actions": ["create", "update", "delete", "restore"],
}


def frame(data: object, id_: int) -> bytes:
    """Frame."""
    return f"id: {id_}\ndata: {json.dumps(data)}\n\n".encode()


def hello(sub: str = "sub-1") -> bytes:
    """Hello."""
    return frame({"subscriber_id": sub}, 0)


def event(action: str = "update", record_id: object = 5, resource: str = "hero") -> bytes:
    """Event."""
    return frame(
        {
            "resource": resource,
            "record_id": record_id,
            "action": action,
            "snapshot": {"id": 5},
            "timestamp": "2026-10-20T00:00:00Z",
        },
        1,
    )


class Body(httpx2.SyncByteStream, httpx2.AsyncByteStream):
    """Yields chunks, then ends or raises."""

    def __init__(self, chunks: list[bytes], error: Exception | None = None) -> None:
        """Create it."""
        self.chunks = chunks
        self.error = error

    def __iter__(self) -> Iterator[bytes]:
        """Protocol method."""
        yield from self.chunks
        if self.error is not None:
            raise self.error

    async def __aiter__(self):  # type: ignore[no-untyped-def]  # noqa: ANN204
        """Protocol method."""
        for chunk in self.chunks:
            yield chunk
        if self.error is not None:
            raise self.error


class Script:
    """One scripted connection per request; records the requests."""

    def __init__(self, *connections: Callable[[httpx2.Request], httpx2.Response]) -> None:
        """Create it."""
        self.connections = list(connections)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        """Protocol method."""
        self.requests.append(request)
        return self.connections.pop(0)(request)


def sse(
    chunks: list[bytes], error: Exception | None = None
) -> Callable[[httpx2.Request], httpx2.Response]:
    """Sse."""

    def make(request: httpx2.Request) -> httpx2.Response:
        """Make."""
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Body(chunks, error)
        )

    return make


def status(code: int) -> Callable[[httpx2.Request], httpx2.Response]:
    """Status."""
    return lambda request: httpx2.Response(code, json={"title": "no"})


def sync_subscriber(script: Script, events: dict[str, object] | None = None) -> EventSubscriber:
    """Sync subscriber."""
    client = httpx2.Client(transport=httpx2.MockTransport(script), base_url="http://t")
    return EventSubscriber(client, events or EVENTS, jitter=lambda: 0.0)


def label(item: object) -> object:
    """Describe a recorded listener call."""
    return (item.action, item.record_id) if isinstance(item, CrudEvent) else item


def task_of(sub: AsyncEventSubscriber) -> asyncio.Task[None] | None:
    """The subscriber's task."""
    return sub._state.task


def run_sync(
    script: Script, stop_after: int, events: dict[str, object] | None = None
) -> tuple[list[object], EventSubscriber]:
    """Run the loop in this thread until `stop_after` records were seen."""
    sub = sync_subscriber(script, events)
    seen: list[object] = []

    def record(item: object) -> None:
        """Record."""
        seen.append(item)
        if len(seen) >= stop_after:
            sub._stop.set()

    sub._listeners.append(_Listener(lambda e: None, None, None))
    sub._listeners.append(_Listener(record, lambda: record("reset"), lambda up: record(("up", up))))
    sub.run()
    return seen, sub


def test_handshake_events_filter_keepalive_and_resume() -> None:
    """Handshake events filter keepalive and resume."""
    script = Script(
        sse(
            [
                hello(),
                b": keep-alive\n\n",
                event("update", 5, resource="villain"),  # other label: filtered
                event("create", 6),
                b"data: not json\n\n",
                b"data: [1]\n\n",
                event("update", None),  # no record id: reset
                event("explode", 5),  # unknown action: reset
            ],
            httpx2.ReadTimeout("silent"),
        ),
        sse([hello("sub-2"), event("delete", 7)]),
    )
    seen, sub = run_sync(script, stop_after=8)
    kinds = [label(s) for s in seen]
    assert kinds == [
        ("up", True),
        ("create", 6),
        "reset",
        "reset",
        ("up", False),
        "reset",  # reconnect
        ("up", True),
        ("delete", 7),
        ("up", False),
    ]
    first, second = script.requests
    assert "subscriber_id" not in first.url.params
    assert second.url.params["subscriber_id"] == "sub-1"
    assert "last-event-id" not in second.headers
    assert first.extensions["timeout"]["read"] == 30  # three keep-alives of 10s
    assert sub._stream.subscriber_id == "sub-2"


def test_no_resume_param_without_persistent_replay() -> None:
    """No resume param without persistent replay."""
    events = {**EVENTS, "replay": "none"}
    script = Script(sse([hello()]), sse([hello("sub-2"), event()]))
    run_sync(script, stop_after=5, events=events)
    assert "subscriber_id" not in script.requests[1].url.params


def test_bad_status_wrong_content_type_and_backoff() -> None:
    """Bad status wrong content type and backoff."""
    wrong = lambda request: httpx2.Response(200, json={})  # noqa: E731
    script = Script(status(401), wrong, sse([hello(), event()]))
    _seen, sub = run_sync(script, stop_after=3)
    assert len(script.requests) == 3
    assert sub._stream.attempt == 0  # reset by the handshake


def test_backoff_doubles_to_the_cap_with_jitter() -> None:
    """Backoff doubles to the cap with jitter."""
    sub = sync_subscriber(Script(), {"path": "/e"})
    stream = sub._stream
    stream.jitter = lambda: 1.0
    delays = [stream.backoff() for _ in range(8)]
    assert delays[:3] == [1.0, 2.0, 4.0]
    assert delays[-1] == 30.0
    stream.jitter = lambda: 0.0
    assert stream.backoff() == 15.0
    assert stream.keepalive == 15.0  # default when the manifest gives none
    assert stream.url == "/e"
    assert stream.resume_param == "subscriber_id"


def test_listener_errors_do_not_kill_the_stream(caplog: pytest.LogCaptureFixture) -> None:
    """Listener errors do not kill the stream."""
    script = Script(sse([hello(), event("create", 1), event("create", 2)]))
    sub = sync_subscriber(script)
    got: list[int] = []

    def bad(e: CrudEvent) -> None:
        """Bad."""
        got.append(e.record_id)
        if len(got) == 2:
            sub._stop.set()
        msg = "listener bug"
        raise RuntimeError(msg)

    with caplog.at_level(logging.ERROR):
        sub.subscribe(bad)  # starts the thread
        assert sub._thread is not None
        sub._thread.join(5)
    assert got == [1, 2]
    assert "event listener failed" in caplog.text


class Gate(httpx2.SyncByteStream):
    """Sends the handshake, then stays open until released."""

    def __init__(self, release: threading.Event, sub: str = "sub-1") -> None:
        """Create it."""
        self.release = release
        self.sub = sub

    def __iter__(self) -> Iterator[bytes]:
        """Protocol method."""
        yield hello(self.sub)
        self.release.wait(5)


class AsyncGate(httpx2.AsyncByteStream):
    """Test helper."""

    def __init__(self, sub: str) -> None:
        """Create it."""
        self.sub = sub

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """Protocol method."""
        yield hello(self.sub)
        await asyncio.Event().wait()


def open_stream(
    stream: httpx2.SyncByteStream | httpx2.AsyncByteStream,
) -> Callable[[httpx2.Request], httpx2.Response]:
    """Open stream."""
    return lambda request: httpx2.Response(
        200, headers={"content-type": "text/event-stream"}, stream=stream
    )


def test_subscribe_refcount_threads_and_unsubscribe() -> None:
    """Subscribe refcount threads and unsubscribe."""
    release = threading.Event()
    connected = threading.Event()
    script = Script(
        lambda r: httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Gate(release)
        )
    )
    sub = sync_subscriber(script)
    ups: list[bool] = []

    def on_status(up: bool) -> None:
        """On status."""
        ups.append(up)
        connected.set()

    un1 = sub.subscribe(lambda e: None, on_status=on_status)
    assert connected.wait(5)
    late: list[bool] = []
    un2 = sub.subscribe(lambda e: None, on_status=late.append)  # joins the live stream
    assert late == [True]
    assert len(script.requests) == 1
    un2()
    un2()  # idempotent
    assert late == [True, False]
    assert not sub._stop.is_set()
    un1()
    assert sub._stop.is_set()
    release.set()
    sub.close()
    assert sub._thread is None
    assert ups == [True, False]


def test_close_without_thread_and_from_inside() -> None:
    """Close without thread and from inside."""
    sub = sync_subscriber(Script())
    sub.close()
    assert sub._stop.is_set()


def test_invalidate_on_events_sync_wiring() -> None:
    """Invalidate on events sync wiring."""
    store = MemoryStore()
    release = threading.Event()
    layer = CacheLayer(store, {"heroes": CachePolicy()})
    for kind, rid in (("list", None), ("record", 5), ("record", 6), ("filters", None)):
        store.set(build_key("heroes", kind, rid, "anon"), CacheEntry(200, (), b"", 0, 0, 0))
    script = Script(
        sse([hello(), event("update", 5)], httpx2.ReadError("cut")),
        open_stream(Gate(release, "s2")),
    )
    sub = sync_subscriber(script)
    seen_active: list[bool] = []
    unsubscribe = invalidate_on_events(layer, sub, "heroes")
    deadline = threading.Event()
    for _ in range(200):
        seen_active.append(layer.events_active("heroes"))
        if len(script.requests) == 2 and layer.events_active("heroes"):
            break
        deadline.wait(0.05)
    assert layer.events_active("heroes")
    # update(5) cleared list+record 5; the reconnect cleared record 6 too; filters stay
    assert store.get(build_key("heroes", "list", None, "anon")) is None
    assert store.get(build_key("heroes", "record", 5, "anon")) is None
    assert store.get(build_key("heroes", "record", 6, "anon")) is None
    assert store.get(build_key("heroes", "filters", None, "anon")) is not None
    unsubscribe()
    assert not layer.events_active("heroes")
    release.set()
    sub.close()


@pytest.mark.asyncio
async def test_async_subscriber_and_wiring(monkeypatch: pytest.MonkeyPatch) -> None:
    """Async subscriber and wiring."""
    store = MemoryStore()
    layer = AsyncCacheLayer(AsyncMemoryStore(store), {"heroes": CachePolicy()})
    for kind, rid in (("list", None), ("record", 5), ("record", 6), ("filters", None)):
        store.set(build_key("heroes", kind, rid, "anon"), CacheEntry(200, (), b"", 0, 0, 0))
    script = Script(
        sse(
            [
                hello(),
                b": keep-alive\n\n",
                event("update", 5, resource="villain"),
                event("update", 5),
                event("update", None),
            ],
            httpx2.ReadTimeout("silent"),
        ),
        status(503),
        open_stream(AsyncGate("s2")),
    )
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(script), base_url="http://t")
    sub = AsyncEventSubscriber(client, EVENTS, jitter=lambda: 0.0)
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def fast_sleep(delay: float) -> None:
        """Fast sleep."""
        sleeps.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    unsubscribe = invalidate_on_events(layer, sub, "heroes")
    for _ in range(500):
        if len(script.requests) == 3 and layer.events_active("heroes"):
            break
        await real_sleep(0.001)
    assert layer.events_active("heroes")
    assert len(script.requests) == 3
    assert script.requests[1].url.params["subscriber_id"] == "sub-1"
    assert script.requests[2].url.params["subscriber_id"] == "sub-1"
    assert store.get(build_key("heroes", "record", 6, "anon")) is None
    assert store.get(build_key("heroes", "filters", None, "anon")) is not None
    assert sleeps[:2] == [0.5, 1.0]
    unsubscribe()
    unsubscribe()
    await sub.aclose()
    assert not layer.events_active("heroes")
    await sub.aclose()
    await client.aclose()


async def ignore_event(e: CrudEvent) -> None:
    """Ignore event."""
    return None


@pytest.mark.asyncio
async def test_async_listener_errors_and_late_subscriber(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Async listener errors and late subscriber."""
    script = Script(sse([hello(), event("create", 1), event("update", None)]))
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(script), base_url="http://t")
    sub = AsyncEventSubscriber(client, EVENTS)
    got = asyncio.Event()

    async def bad(e: CrudEvent) -> None:
        """Bad."""
        got.set()
        msg = "listener bug"
        raise RuntimeError(msg)

    async def on_reset() -> None:
        """On reset."""
        got.set()
        msg = "reset bug"
        raise RuntimeError(msg)

    with caplog.at_level(logging.ERROR):
        sub.subscribe(ignore_event)  # no reset or status callbacks
        un = sub.subscribe(bad, on_reset=on_reset)
        await asyncio.wait_for(got.wait(), 5)
        un()
    await sub.aclose()
    assert "event listener failed" in caplog.text
    await client.aclose()


def test_run_stops_at_once_when_already_stopped_and_mid_stream() -> None:
    """Run stops at once when already stopped and mid stream."""
    script = Script(sse([hello(), event("create", 1), event("create", 2)]))
    seen, _ = run_sync(script, stop_after=2)  # up, then the first event stops the loop
    assert [label(s) for s in seen] == [
        ("up", True),
        ("create", 1),
        ("up", False),
    ]
    idle = sync_subscriber(Script())
    idle._stop.set()
    idle.run()


def test_default_jitter_is_a_unit_random() -> None:
    """Default jitter is a unit random."""
    sub = EventSubscriber(httpx2.Client(), EVENTS)
    assert 0.5 <= sub._stream.backoff() <= 1.0
    asub = AsyncEventSubscriber(httpx2.AsyncClient(), EVENTS)
    assert 0.5 <= asub._stream.backoff() <= 1.0


def test_unsubscribing_a_listener_without_status() -> None:
    """Unsubscribing a listener without status."""
    sub = sync_subscriber(Script(sse([hello()])))
    sub._connected = True
    sub._thread = threading.Thread()  # pretend a thread runs
    un = sub.subscribe(lambda e: None)
    un()
    assert sub._stop.is_set()


@pytest.mark.asyncio
async def test_async_refcount_keeps_stream_until_last_listener_leaves() -> None:
    """Async refcount keeps stream until last listener leaves."""
    script = Script(open_stream(AsyncGate("s1")))
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(script), base_url="http://t")
    sub = AsyncEventSubscriber(client, EVENTS)

    async def ignore(e: CrudEvent) -> None:
        """Ignore."""
        return None

    first = sub.subscribe(ignore)
    second = sub.subscribe(ignore)
    await asyncio.sleep(0.01)
    assert len(script.requests) == 1
    first()
    assert task_of(sub) is not None
    second()
    await sub.aclose()
    assert task_of(sub) is None
    await client.aclose()


def test_a_non_http_error_closes_the_gate_and_a_new_subscribe_restarts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The loop dying of a bug still tells listeners; subscribing again starts a new stream."""
    release = threading.Event()
    layer = CacheLayer(MemoryStore(), {"heroes": CachePolicy()})
    script = Script(
        sse([hello()], RuntimeError("decoder bug")),
        open_stream(Gate(release, "s2")),
    )
    sub = sync_subscriber(script)
    with caplog.at_level(logging.ERROR):
        unsubscribe = invalidate_on_events(layer, sub, "heroes")
        thread = sub._thread
        assert thread is not None
        thread.join(5)
    assert not thread.is_alive()
    assert "event stream /api/heroes/events died" in caplog.text
    assert not layer.events_active("heroes")  # on_status(False) fired on the way out
    assert sub._thread is None
    assert not sub._connected
    restarted = threading.Event()

    def on_status(up: bool) -> None:
        """Signal the restart."""
        if up:
            restarted.set()

    sub.subscribe(lambda e: None, on_status=on_status)
    assert restarted.wait(5)
    assert len(script.requests) == 2
    unsubscribe()
    release.set()
    sub.close()


class Blocking(httpx2.SyncByteStream):
    """Sends the handshake, then blocks until the response is closed (like a socket read)."""

    def __init__(self) -> None:
        """Create it."""
        self.closed = threading.Event()

    def __iter__(self) -> Iterator[bytes]:
        """Protocol method."""
        yield hello()
        self.closed.wait(10)
        raise httpx2.ReadError("closed under the reader")

    def close(self) -> None:
        """Protocol method."""
        self.closed.set()


def _started(sub: EventSubscriber) -> threading.Thread:
    up = threading.Event()
    sub.subscribe(lambda e: None, on_status=lambda ok: up.set() if ok else None)
    assert up.wait(5)
    thread = sub._thread
    assert thread is not None
    return thread


def test_close_unblocks_the_reader_and_joins_the_thread() -> None:
    """Close closes the live response, so the reader does not wait for a frame."""
    body = Blocking()
    sub = sync_subscriber(Script(open_stream(body)))
    thread = _started(sub)
    sub.close(timeout=3)
    assert body.closed.is_set()
    assert not thread.is_alive()
    assert sub._thread is None


def test_the_last_unsubscribe_unblocks_the_reader() -> None:
    """Unsubscribing the last listener also closes the stream."""
    body = Blocking()
    sub = sync_subscriber(Script(open_stream(body)))
    up = threading.Event()
    unsubscribe = sub.subscribe(lambda e: None, on_status=lambda ok: up.set() if ok else None)
    assert up.wait(5)
    thread = sub._thread
    assert thread is not None
    unsubscribe()
    thread.join(3)
    assert body.closed.is_set()
    assert not thread.is_alive()


def test_close_warns_when_the_thread_does_not_stop(caplog: pytest.LogCaptureFixture) -> None:
    """A reader that ignores the close is reported, not silently abandoned."""
    release = threading.Event()
    sub = sync_subscriber(Script(open_stream(Gate(release))))
    thread = _started(sub)
    with caplog.at_level(logging.WARNING):
        sub.close(timeout=0.05)
    assert "did not stop" in caplog.text
    release.set()
    thread.join(5)


@pytest.mark.asyncio
async def test_async_non_http_error_closes_the_gate_and_resubscribe_restarts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An async loop dying of a bug still sends on_status(False); subscribing again restarts."""
    layer = AsyncCacheLayer(AsyncMemoryStore(MemoryStore()), {"heroes": CachePolicy()})
    script = Script(sse([hello()], RuntimeError("decoder bug")), open_stream(AsyncGate("s2")))
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(script), base_url="http://t")
    sub = AsyncEventSubscriber(client, EVENTS)
    with caplog.at_level(logging.ERROR):
        unsubscribe = invalidate_on_events(layer, sub, "heroes")
        task = task_of(sub)
        assert task is not None
        await asyncio.gather(task, return_exceptions=True)
    assert "died" in caplog.text
    assert not layer.events_active("heroes")
    assert task_of(sub) is None
    unsubscribe()
    invalidate_on_events(layer, sub, "heroes")
    for _ in range(500):
        if layer.events_active("heroes"):
            break
        await asyncio.sleep(0.001)
    assert layer.events_active("heroes")
    assert len(script.requests) == 2
    await sub.aclose()
    assert not layer.events_active("heroes")
    await client.aclose()


def test_subscribe_while_the_last_reader_is_stopping_gets_a_live_reader() -> None:
    """A subscribe between the last unsubscribe and the old thread's exit starts a new stream."""
    release_old, release_new = threading.Event(), threading.Event()
    script = Script(open_stream(Gate(release_old, "old")), open_stream(Gate(release_new, "new")))
    sub = sync_subscriber(script)
    first_up = threading.Event()
    unsubscribe = sub.subscribe(lambda e: None, on_status=lambda ok: first_up.set() if ok else None)
    assert first_up.wait(5)
    old = sub._thread
    assert old is not None
    unsubscribe()  # the old reader is stuck in its stream and has not exited yet
    assert old.is_alive()

    ups: list[bool] = []
    second_up = threading.Event()

    def on_status(ok: bool) -> None:
        """Record."""
        ups.append(ok)
        if ok:
            second_up.set()

    sub.subscribe(lambda e: None, on_status=on_status)
    assert sub._thread is not old
    assert second_up.wait(5)
    release_old.set()
    old.join(5)
    assert not old.is_alive()
    assert sub._thread is not None
    assert sub._thread.is_alive()  # the old reader's cleanup did not clear the new one
    assert sub._connected
    assert ups == [True]  # nor flip the status
    assert len(script.requests) == 2
    release_new.set()
    sub.close()


@pytest.mark.asyncio
async def test_async_subscribe_while_the_last_reader_is_stopping_gets_a_live_reader() -> None:
    """The async twin: cancel is not exit, a subscribe in between still gets a stream."""
    script = Script(open_stream(AsyncGate("old")), open_stream(AsyncGate("new")))
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(script), base_url="http://t")
    sub = AsyncEventSubscriber(client, EVENTS)
    ups: list[bool] = []

    async def ignore(e: CrudEvent) -> None:
        """Ignore."""

    async def on_status(ok: bool) -> None:
        """Record."""
        ups.append(ok)

    first = sub.subscribe(ignore, on_status=on_status)
    await asyncio.sleep(0.01)
    old = task_of(sub)
    first()
    assert old is not None
    ups.clear()
    sub.subscribe(ignore, on_status=on_status)  # before the cancelled task has run again
    await asyncio.sleep(0.05)
    assert old.done()
    new = task_of(sub)
    assert new is not None
    assert new is not old
    assert not new.done()
    assert len(script.requests) == 2
    assert ups == [True]
    await sub.aclose()
    await client.aclose()


@pytest.mark.asyncio
async def test_async_listener_joining_a_connected_stream_gets_status_true() -> None:
    """As with the sync subscriber, a late listener learns that the stream is already up."""
    script = Script(open_stream(AsyncGate("s1")))
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(script), base_url="http://t")
    sub = AsyncEventSubscriber(client, EVENTS)
    early: list[bool] = []
    late: list[bool] = []

    async def ignore(e: CrudEvent) -> None:
        """Ignore."""

    async def on_early(ok: bool) -> None:
        """Record."""
        early.append(ok)

    async def on_late(ok: bool) -> None:
        """Record."""
        late.append(ok)

    sub.subscribe(ignore, on_status=on_early)
    await asyncio.sleep(0.01)
    assert early == [True]
    leave = sub.subscribe(ignore, on_status=on_late)
    await asyncio.sleep(0.01)
    assert late == [True]
    quick = sub.subscribe(ignore, on_status=on_late)
    quick()  # gone before its notification ran: no call
    sub.subscribe(ignore)  # a listener without on_status joins quietly
    leave()
    await asyncio.sleep(0.01)
    assert late == [True]
    await sub.aclose()
    await client.aclose()


def test_a_reader_stopped_before_its_response_is_published_publishes_nothing() -> None:
    """A replaced or stopped reader that just connected leaves without touching the state."""
    sub = sync_subscriber(Script(open_stream(Gate(threading.Event()))))
    stopped = threading.Event()
    stopped.set()
    sub._read_once(stopped)
    assert sub._response is None
