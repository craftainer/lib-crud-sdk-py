"""The asynchronous cache hook, including stale-while-revalidate."""

import asyncio
import logging

import httpx2
import pytest

from craftainer_crud_sdk.cache import (
    AsyncCacheLayer,
    AsyncMemoryStore,
    CachePolicy,
    MemoryStore,
    build_key,
)
from tests.unit.cache.harness import ETAG, AsyncRunner, Clock, Server, make_request

pytestmark = pytest.mark.asyncio

POLICY = CachePolicy(max_age=60, stats_ttl=30, stale_while_revalidate=120)


def setup(*, bind: bool = True) -> tuple[AsyncRunner, Server, MemoryStore, AsyncCacheLayer, Clock]:
    """Setup."""
    clock = Clock()
    store = MemoryStore(clock=clock)
    layer = AsyncCacheLayer(AsyncMemoryStore(store), {"heroes": POLICY}, clock=clock)
    layer.set_events_active("heroes", True)
    server = Server()
    runner = AsyncRunner(layer, server)
    if bind:
        layer.bind(runner.raw)
    return runner, server, store, layer, clock


def status_of(response: httpx2.Response) -> object:
    """Status of."""
    return response.extensions.get("craftainer_cache")


async def drain(layer: AsyncCacheLayer) -> None:
    """Drain."""
    await asyncio.gather(*layer._tasks)


async def test_hit_and_revalidation() -> None:
    """Hit and revalidation."""
    runner, server, _, layer, clock = setup()
    assert status_of(await runner.send(make_request())) == "miss"
    assert status_of(await runner.send(make_request())) == "hit"
    layer.set_events_active("heroes", False)
    revalidated = await runner.send(make_request())
    assert status_of(revalidated) == "revalidated"
    assert revalidated.headers["x-new"] == "1"
    assert len(server.calls) == 2
    clock.now += 1
    assert await runner.send(make_request(context=False)) is not None


async def test_stale_while_revalidate_serves_stale_then_refreshes() -> None:
    """Stale while revalidate serves stale then refreshes."""
    runner, server, _, layer, clock = setup()
    await runner.send(make_request())
    clock.now += 100  # past max_age, inside the SWR window
    server.etag = '"v2"'
    server.body = {"id": 1, "name": "new"}
    stale = await runner.send(make_request())
    assert status_of(stale) == "stale"
    assert stale.json() == {"id": 1, "name": "a"}
    again = await runner.send(make_request())  # a second read while one is in flight
    assert status_of(again) == "stale"
    await drain(layer)
    assert len(server.calls) == 2  # one background request only
    assert server.calls[1].headers["if-none-match"] == ETAG
    fresh = await runner.send(make_request())
    assert status_of(fresh) == "hit"
    assert fresh.json() == {"id": 1, "name": "new"}
    # a 304 in the background just extends freshness
    clock.now += 100
    assert status_of(await runner.send(make_request())) == "stale"
    await drain(layer)
    assert status_of(await runner.send(make_request())) == "hit"


async def test_no_swr_without_bind_or_after_window() -> None:
    """No swr without bind or after window."""
    runner, server, _, _, clock = setup(bind=False)
    await runner.send(make_request())
    clock.now += 100
    assert status_of(await runner.send(make_request())) == "revalidated"
    assert len(server.calls) == 2
    runner2, _, _, layer2, clock2 = setup()
    await runner2.send(make_request())
    clock2.now += 500  # beyond fresh + swr
    assert status_of(await runner2.send(make_request())) == "revalidated"
    assert not layer2._tasks


async def test_background_failure_is_logged_and_clears_inflight(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Background failure is logged and clears inflight."""
    runner, _, _, layer, clock = setup()

    async def broken(request: httpx2.Request) -> httpx2.Response:
        """Broken."""
        raise httpx2.ConnectError("boom", request=request)

    await runner.send(make_request())
    layer.bind(broken)
    clock.now += 100
    with caplog.at_level(logging.ERROR):
        assert status_of(await runner.send(make_request())) == "stale"
        await drain(layer)
    assert "background revalidation" in caplog.text
    assert not layer._inflight


async def test_aclose_cancels_background_work() -> None:
    """Aclose cancels background work."""
    runner, _, _, layer, clock = setup()
    await runner.send(make_request())
    gate = asyncio.Event()

    async def slow(request: httpx2.Request) -> httpx2.Response:
        """Slow."""
        await gate.wait()
        return httpx2.Response(500)

    layer.bind(slow)
    clock.now += 100
    await runner.send(make_request())
    await asyncio.sleep(0)
    await layer.aclose()
    assert not layer._tasks


async def test_writes_and_events() -> None:
    """Writes and events."""
    runner, server, store, layer, _ = setup()
    await runner.send(make_request("record", record_id=7, query="id=7"))
    await runner.send(make_request("list"))
    server.body = {"id": 7, "name": "p"}
    server.etag = '"v2"'
    await runner.send(make_request("update", method="PATCH", record_id=7))
    assert store.get(build_key("heroes", "list", None, "anon")) is None
    assert await layer.cached_etag("heroes", 7, "anon") == '"v2"'
    assert await layer.cached_etag("heroes", 99, "anon") is None
    await runner.send(make_request("list"))
    server.status = 412
    await runner.send(make_request("update", method="PATCH", record_id=7))
    assert await layer.cached_etag("heroes", 7, "anon") is None
    server.status = 204
    await runner.send(make_request("delete", method="DELETE", record_id=7))
    await layer.invalidate_event("heroes", "create", 1)
    await layer.clear_resource("heroes")
    assert len(store) == 0
    await runner.send(make_request("list"))
    await layer.clear("v1/heroes/")
    assert len(store) == 0
    await runner.send(make_request("list"))
    await layer.clear()
    assert len(store) == 0
    assert layer.events_active("heroes")


async def test_404_evicts_and_non_cached_passes() -> None:
    """404 evicts and non cached passes."""
    runner, server, store, layer, _ = setup()
    await runner.send(make_request())
    layer.set_events_active("heroes", False)
    server.status = 404
    server.etag = None
    await runner.send(make_request())
    assert len(store) == 0
    server.status = 200
    await runner.send(make_request(resource="other"))


async def test_stale_entry_without_validator_is_refetched_plainly() -> None:
    """Stale entry without validator is refetched plainly."""
    clock = Clock()
    layer = AsyncCacheLayer(
        AsyncMemoryStore(MemoryStore(clock=clock)),
        {"heroes": CachePolicy(max_age=10, stale_while_revalidate=50, etag=False)},
        clock=clock,
    )
    layer.set_events_active("heroes", True)
    server = Server()
    runner = AsyncRunner(layer, server)
    layer.bind(runner.raw)
    await runner.send(make_request())
    clock.now += 20
    assert status_of(await runner.send(make_request())) == "stale"
    await drain(layer)
    assert len(server.calls) == 2
    assert "if-none-match" not in server.calls[1].headers
