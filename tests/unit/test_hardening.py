"""PY4 hardening: each test pins one review finding of the SDK's third round."""

import asyncio
from collections.abc import Awaitable, Callable, Generator

import fakeredis
import fakeredis.aioredis
import httpx2
import pytest

from craftainer_crud_sdk import (
    AsyncCrudClient,
    AsyncMemoryStore,
    CrudClient,
    MemoryStore,
    aenable_cache,
    cache_scope,
    enable_cache,
)
from craftainer_crud_sdk.auth import BearerAuth, call_provider, static_token
from craftainer_crud_sdk.cache import AsyncCacheLayer, CacheLayer, CachePolicy, build_key
from craftainer_crud_sdk.cache.redis import AsyncRedisStore, RedisStore
from craftainer_crud_sdk.client import _decode
from craftainer_crud_sdk.errors import CrudError, ResponseError
from craftainer_crud_sdk.events import AsyncEventSubscriber, EventSubscriber, invalidate_on_events
from craftainer_crud_sdk.interfaces import CacheEntry, Op
from tests.unit.cache.harness import AsyncRunner, Clock, Server, SyncRunner, make_request
from tests.unit.test_events import EVENTS, AsyncGate, Script, open_stream

from ._fake_server import FakeServer
from ._support import BASE, make_transport

STATS_POLICY = CachePolicy(max_age=60, stats_ttl=30, stale_while_revalidate=120)


def cache_status(response: httpx2.Response) -> object:
    """How the cache answered."""
    return response.extensions.get("craftainer_cache")


# -- a. SWR only while the stream is up, stats/predict included ---------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["stats", "predict"])
async def test_swr_of_stats_and_predict_needs_the_stream(op: Op) -> None:
    """A stale stats entry is not served stale once the stream is down; ttl freshness stays."""
    clock = Clock()
    store = MemoryStore(clock=clock)
    layer = AsyncCacheLayer(AsyncMemoryStore(store), {"heroes": STATS_POLICY}, clock=clock)
    runner = AsyncRunner(layer, Server())
    layer.bind(runner.raw)
    layer.set_events_active("heroes", True)
    assert cache_status(await runner.send(make_request(op))) == "miss"
    clock.now += 40  # past stats_ttl, inside the swr window
    layer.set_events_active("heroes", False)
    assert cache_status(await runner.send(make_request(op))) == "revalidated"


@pytest.mark.asyncio
async def test_stats_stored_without_a_stream_get_ttl_but_no_swr() -> None:
    """Without a stream, stats are fresh for stats_ttl and never stale-while-revalidate."""
    clock = Clock()
    layer = AsyncCacheLayer(
        AsyncMemoryStore(MemoryStore(clock=clock)), {"heroes": STATS_POLICY}, clock=clock
    )
    runner = AsyncRunner(layer, Server())
    layer.bind(runner.raw)
    assert cache_status(await runner.send(make_request("stats"))) == "miss"
    clock.now += 10
    assert cache_status(await runner.send(make_request("stats"))) == "hit"
    clock.now += 30
    assert cache_status(await runner.send(make_request("stats"))) == "revalidated"
    layer.set_events_active("heroes", True)  # the entry was stored without a grant: still none
    clock.now += 40
    assert cache_status(await runner.send(make_request("stats"))) == "revalidated"
    await layer.aclose()


# -- b. the first handshake clears the resource -------------------------------------------------


@pytest.mark.asyncio
async def test_the_first_async_handshake_clears_entries_from_before_the_process() -> None:
    """A persistent store may hold entries from an earlier process: connect clears them."""
    store = MemoryStore()
    layer = AsyncCacheLayer(AsyncMemoryStore(store), {"heroes": CachePolicy()})
    for kind, rid in (("list", None), ("record", 6), ("filters", None)):
        store.set(build_key("heroes", kind, rid, "anon"), CacheEntry(200, (), b"", 0, 0, 0))
    script = Script(open_stream(AsyncGate("s1")))
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(script), base_url="http://t")
    sub = AsyncEventSubscriber(client, EVENTS, jitter=lambda: 0.0)
    unsubscribe = invalidate_on_events(layer, sub, "heroes")
    for _ in range(500):
        if layer.events_active("heroes"):
            break
        await asyncio.sleep(0.001)
    assert len(script.requests) == 1  # no reconnect happened
    assert store.get(build_key("heroes", "list", None, "anon")) is None
    assert store.get(build_key("heroes", "record", 6, "anon")) is None
    assert store.get(build_key("heroes", "filters", None, "anon")) is not None
    unsubscribe()
    await sub.aclose()
    await client.aclose()


# -- c. layer / subscriber mismatch -------------------------------------------------------------


def test_invalidate_on_events_rejects_a_sync_async_mismatch() -> None:
    """Wiring a sync layer to an async subscriber (or the reverse) is a TypeError."""
    sync_layer = CacheLayer(MemoryStore(), {})
    async_layer = AsyncCacheLayer(AsyncMemoryStore(MemoryStore()), {})
    sync_sub = EventSubscriber(httpx2.Client(), EVENTS)
    async_sub = AsyncEventSubscriber(httpx2.AsyncClient(), EVENTS)
    with pytest.raises(TypeError, match="sync layer with the sync subscriber"):
        invalidate_on_events(sync_layer, async_sub, "heroes")  # type: ignore[call-overload]
    with pytest.raises(TypeError, match="CacheLayer"):
        invalidate_on_events(async_layer, sync_sub, "heroes")  # type: ignore[call-overload]
    assert not sync_layer.events_active("heroes")
    assert not async_layer.events_active("heroes")


# -- d. only write methods clear in begin -------------------------------------------------------


@pytest.mark.parametrize("method", ["HEAD", "OPTIONS"])
def test_head_and_options_pass_through_without_clearing(method: str) -> None:
    """A HEAD or OPTIONS never invalidates what is stored."""
    store = MemoryStore()
    layer = CacheLayer(store, {"heroes": CachePolicy()})
    server = Server()
    runner = SyncRunner(layer, server)
    runner.send(make_request())
    key = build_key("heroes", "list", None, "anon")
    assert store.get(key) is not None
    generation = layer._core.generation("heroes")
    response = runner.send(make_request("create", method=method))
    assert response.status_code == 200
    assert store.get(key) is not None
    assert layer._core.generation("heroes") == generation


@pytest.mark.parametrize("method", ["POST", "PATCH", "PUT", "DELETE"])
def test_write_methods_still_clear(method: str) -> None:
    """The four write methods clear the collections."""
    store = MemoryStore()
    layer = CacheLayer(store, {"heroes": CachePolicy()})
    runner = SyncRunner(layer, Server())
    runner.send(make_request())
    runner.send(make_request("create", method=method))
    assert store.get(build_key("heroes", "list", None, "anon")) is None


# -- e. _inflight cleanup ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_revalidation_cancelled_before_it_starts_leaves_no_inflight_key() -> None:
    """The task never ran its body, yet its key must not block later revalidations."""
    clock = Clock()
    layer = AsyncCacheLayer(
        AsyncMemoryStore(MemoryStore(clock=clock)), {"heroes": STATS_POLICY}, clock=clock
    )
    server = Server()
    runner = AsyncRunner(layer, server)
    layer.bind(runner.raw)
    layer.set_events_active("heroes", True)
    await runner.send(make_request())
    clock.now += 90
    assert cache_status(await runner.send(make_request())) == "stale"
    assert len(layer._inflight) == 1
    for task in list(layer._tasks):
        task.cancel()  # before the loop ran it
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not layer._inflight
    assert not layer._tasks
    assert cache_status(await runner.send(make_request())) == "stale"
    assert len(layer._inflight) == 1  # scheduled again, not stuck
    await layer.aclose()


@pytest.mark.asyncio
async def test_aclose_clears_inflight() -> None:
    """After aclose nothing is in flight, whatever state the tasks were in."""
    clock = Clock()
    layer = AsyncCacheLayer(
        AsyncMemoryStore(MemoryStore(clock=clock)), {"heroes": STATS_POLICY}, clock=clock
    )
    runner = AsyncRunner(layer, Server())
    layer.bind(runner.raw)
    layer.set_events_active("heroes", True)
    await runner.send(make_request())
    clock.now += 90
    await runner.send(make_request())
    await layer.aclose()
    assert not layer._inflight
    assert not layer._tasks


# -- f. empty Redis namespace ------------------------------------------------------------------


def test_redis_stores_reject_an_empty_namespace() -> None:
    """clear() would reach every key of the database."""
    with pytest.raises(ValueError, match="namespace"):
        RedisStore(fakeredis.FakeRedis(), namespace="")
    with pytest.raises(ValueError, match="namespace"):
        AsyncRedisStore(fakeredis.aioredis.FakeRedis(), namespace="")


# -- g. scope() is the transport's header logic -----------------------------------------------


class Lookups:
    """Records the scope the sync auto-If-Match lookup is asked for."""

    def __init__(self, inner: Callable[[str, int, str, str | None], str | None]) -> None:
        """Wrap `inner`."""
        self.inner = inner
        self.scopes: list[str] = []

    def __call__(
        self, resource: str, record_id: int, scope: str, version: str | None
    ) -> str | None:
        """Lookup."""
        self.scopes.append(scope)
        return self.inner(resource, record_id, scope, version)


class AsyncLookups:
    """Async twin of `Lookups`."""

    def __init__(self, inner: Callable[[str, int, str, str | None], Awaitable[str | None]]) -> None:
        """Wrap `inner`."""
        self.inner = inner
        self.scopes: list[str] = []

    async def __call__(
        self, resource: str, record_id: int, scope: str, version: str | None
    ) -> str | None:
        """Lookup."""
        self.scopes.append(scope)
        return await self.inner(resource, record_id, scope, version)


@pytest.mark.parametrize("token", [None, ""])
def test_sync_scope_keeps_a_static_header_when_the_provider_returns_nothing(
    token: str | None,
) -> None:
    """scope() equals the scope the cache key (auto If-Match lookup) was built with."""

    def provider(*, refresh: bool = False) -> str | None:
        return token

    with CrudClient(
        BASE,
        transport=FakeServer().transport(),
        token_provider=provider,
        headers={"Authorization": "Bearer static"},
    ) as client:
        layer = enable_cache(client, MemoryStore(), events=False, auto_if_match=True)
        lookups = Lookups(layer.cached_etag)
        client.set_etag_lookup(lookups)
        heroes = client.resource("heroes")
        heroes.get(1)
        heroes.update(1, {"name": "Grace"})
        assert client.scope() == cache_scope("static")
        assert lookups.scopes == [client.scope()]


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [None, ""])
async def test_async_scope_keeps_a_static_header_when_the_provider_returns_nothing(
    token: str | None,
) -> None:
    """Async twin of the sync test."""

    async def provider(*, refresh: bool = False) -> str | None:
        return token

    async with AsyncCrudClient(
        BASE,
        transport=FakeServer().transport(),
        token_provider=provider,
        headers={"Authorization": "Bearer static"},
    ) as client:
        layer = await aenable_cache(client, AsyncMemoryStore(), events=False, auto_if_match=True)
        lookups = AsyncLookups(layer.cached_etag)
        client.set_etag_lookup(lookups)
        heroes = await client.resource("heroes")
        await heroes.get(1)
        await heroes.update(1, {"name": "Grace"})
        assert await client.scope() == cache_scope("static")
        assert lookups.scopes == [await client.scope()]


# -- h. a malformed 2xx body -------------------------------------------------------------------


@pytest.mark.parametrize("body", [b"<html>not json</html>", b"\xff\xfe{", b'{"a":'])
def test_a_malformed_success_body_is_a_problem_error(body: bytes) -> None:
    """Never a raw JSONDecodeError/UnicodeDecodeError out of a 200."""
    transport = make_transport(lambda request: httpx2.Response(200, content=body))
    with CrudClient(BASE, transport=transport) as client, pytest.raises(ResponseError) as caught:
        client.resource("heroes").get(1)
    assert isinstance(caught.value, CrudError)
    assert caught.value.status == 200
    assert caught.value.response is not None
    assert isinstance(caught.value.__cause__, ValueError)


@pytest.mark.asyncio
async def test_async_malformed_success_body_is_a_problem_error() -> None:
    """Async twin."""
    transport = make_transport(lambda request: httpx2.Response(200, content=b"nope"))
    async with AsyncCrudClient(BASE, transport=transport) as client:
        heroes = await client.resource("heroes")
        with pytest.raises(ResponseError):
            await heroes.get(1)


# -- i. one provider calling convention ---------------------------------------------------------


class Recording:
    """A provider that records how it was called; `aio` is its async twin."""

    def __init__(self) -> None:
        """Create it."""
        self.calls: list[dict[str, bool]] = []

    def __call__(self, **kwargs: bool) -> str:
        """Sync."""
        self.calls.append(kwargs)
        return f"tok{len(self.calls)}"

    async def aio(self, **kwargs: bool) -> str:
        """Async."""
        return self(**kwargs)


def _unauthorized_once() -> Callable[[httpx2.Request], httpx2.Response]:
    seen = {"n": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["n"] += 1
        return httpx2.Response(401 if seen["n"] == 1 else 200)

    return handler


def test_sync_and_async_flows_call_the_provider_the_same_way() -> None:
    """`provider()` first, `provider(refresh=True)` after a 401: no refresh=False keyword."""
    sync_rec = Recording()
    with httpx2.Client(
        transport=httpx2.MockTransport(_unauthorized_once()), auth=BearerAuth(sync_rec)
    ) as client:
        client.get("http://x/")

    async def run() -> list[dict[str, bool]]:
        rec = Recording()
        async with httpx2.AsyncClient(
            transport=httpx2.MockTransport(_unauthorized_once()), auth=BearerAuth(rec.aio)
        ) as aclient:
            await aclient.get("http://x/")
        return rec.calls

    assert sync_rec.calls == [{}, {"refresh": True}]
    assert asyncio.run(run()) == sync_rec.calls


def test_scope_calls_the_provider_like_the_flow() -> None:
    """scope() is a plain `provider()` call too."""
    rec = Recording()
    with CrudClient(BASE, transport=FakeServer().transport(), token_provider=rec) as client:
        client.scope()
    assert rec.calls == [{}]


def test_a_sync_client_rejects_an_async_provider() -> None:
    """Calling a coroutine function from the sync flow would sign with a coroutine."""
    rec = Recording()
    with (
        httpx2.Client(
            transport=httpx2.MockTransport(lambda request: httpx2.Response(200)),
            auth=BearerAuth(rec.aio),
        ) as client,
        pytest.raises(TypeError, match="synchronous token provider"),
    ):
        client.get("http://x/")
    with (
        CrudClient(
            BASE,
            transport=FakeServer().transport(),
            token_provider=rec.aio,  # type: ignore[arg-type]
        ) as bad,
        pytest.raises(TypeError, match="synchronous token provider"),
    ):
        bad.scope()


@pytest.mark.asyncio
async def test_the_async_client_accepts_a_sync_provider() -> None:
    """A plain callable works too; its result is not awaited."""
    async with AsyncCrudClient(
        BASE, transport=FakeServer().transport(), token_provider=static_token("t")
    ) as client:
        assert await client.scope() == cache_scope("t")


class _Awaitable:
    """An awaitable that is not a coroutine."""

    def __await__(self) -> Generator[None]:
        """Never awaited by the sync flow."""
        yield  # pragma: no cover  # reached only if the sync flow wrongly awaited it


def test_a_non_coroutine_awaitable_from_a_sync_client_is_rejected_too() -> None:
    """Only a real coroutine needs closing; any awaitable is refused."""
    with pytest.raises(TypeError, match="synchronous token provider"):
        call_provider(lambda **_: _Awaitable())  # type: ignore[arg-type]


def test_decode_empty_bodies_are_none() -> None:
    """204 and an empty 2xx body decode to None; only a non-empty malformed one is an error."""
    assert _decode(httpx2.Response(204)) is None
    assert _decode(httpx2.Response(200, content=b"")) is None
    assert ResponseError("x").status is None


@pytest.mark.asyncio
async def test_a_finished_revalidation_does_not_free_a_newer_tasks_key() -> None:
    """The done callback only removes its own task from `_inflight`."""
    clock = Clock()
    layer = AsyncCacheLayer(
        AsyncMemoryStore(MemoryStore(clock=clock)), {"heroes": STATS_POLICY}, clock=clock
    )
    runner = AsyncRunner(layer, Server())
    layer.bind(runner.raw)
    layer.set_events_active("heroes", True)
    await runner.send(make_request())
    clock.now += 90
    await runner.send(make_request())
    (key,) = layer._inflight
    newer = asyncio.ensure_future(asyncio.sleep(10))
    layer._inflight[key] = newer
    await asyncio.gather(*layer._tasks)
    await asyncio.sleep(0)
    assert layer._inflight[key] is newer
    newer.cancel()
    await layer.aclose()
