"""A store whose clear fails leaves the resource not fresh until a reset succeeds."""

from collections.abc import Sequence

import httpx2
import pytest

from craftainer_crud_sdk.cache import AsyncCacheLayer, CacheLayer, CachePolicy, MemoryStore
from craftainer_crud_sdk.interfaces import CacheEntry
from tests.unit.cache.harness import AsyncRunner, Clock, Server, SyncRunner, make_request

POLICY = CachePolicy(max_age=60, stats_ttl=30, stale_while_revalidate=120)


class Flaky:
    """A store whose clears fail (returning False, as RedisStore does) while `ok` is off."""

    def __init__(self, clock: Clock, *, many: bool = True) -> None:
        """Wrap a MemoryStore; `many=False` hides `clear_many` (per-prefix fallback)."""
        self.ok = True
        self.store = MemoryStore(clock=clock)
        if not many:
            self.clear_many = None  # type: ignore[assignment]

    def get(self, key: str) -> CacheEntry | None:
        """Delegate."""
        return self.store.get(key)

    def set(self, key: str, entry: CacheEntry, ttl: float | None = None) -> None:
        """Delegate."""
        self.store.set(key, entry, ttl)

    def delete(self, key: str) -> None:
        """Delegate."""
        self.store.delete(key)

    def clear(self, prefix: str | None = None) -> bool | None:
        """Fail without clearing, or clear."""
        if not self.ok:
            return False
        self.store.clear(prefix)
        return None

    def clear_many(self, prefixes: Sequence[str]) -> bool | None:
        """Fail without clearing, or clear."""
        if not self.ok:
            return False
        self.store.clear_many(prefixes)
        return None


def sync_setup() -> tuple[SyncRunner, Server, Flaky, CacheLayer, Clock]:
    """A layer over a Flaky store with the heroes stream up."""
    clock = Clock()
    store = Flaky(clock)
    layer = CacheLayer(store, {"heroes": POLICY}, clock=clock)
    layer.set_events_active("heroes", True)
    server = Server()
    return SyncRunner(layer, server), server, store, layer, clock


def status(response: httpx2.Response) -> object:
    """The cache status."""
    return response.extensions.get("craftainer_cache")


def test_a_failed_event_clear_revalidates_until_the_next_reset() -> None:
    """After a failed clear every read revalidates, even with the stream up; a reset recovers."""
    runner, _, store, layer, _ = sync_setup()
    runner.send(make_request())
    assert status(runner.send(make_request())) == "hit"
    store.ok = False
    layer.invalidate_event("heroes", "create", 1)
    assert layer.events_active("heroes")  # the stream itself is fine
    assert status(runner.send(make_request())) == "revalidated"
    assert status(runner.send(make_request())) == "revalidated"  # not even a new entry is fresh
    layer.clear_resource("heroes")  # the reset fails too: still not fresh
    assert status(runner.send(make_request())) == "revalidated"
    store.ok = True
    layer.invalidate_event("heroes", "create", 1)  # a partial clear does not recover
    assert status(runner.send(make_request())) == "miss"
    assert status(runner.send(make_request())) == "revalidated"
    layer.clear_resource("heroes")  # the first successful reset
    runner.send(make_request())
    assert status(runner.send(make_request())) == "hit"


def test_other_resources_and_stats_follow_the_resource_flag() -> None:
    """Only the affected resource is not fresh; its stats entries stop being fresh too."""
    runner, _, store, layer, _ = sync_setup()
    layer.set_events_active("villains", True)
    runner.send(make_request("stats"))
    assert status(runner.send(make_request("stats"))) == "hit"
    store.ok = False
    layer.clear_resource("heroes")
    assert status(runner.send(make_request("stats"))) == "revalidated"
    assert layer.events_active("villains")
    layer.clear_resource("villains")  # flags only the resource it names
    assert status(runner.send(make_request("stats"))) == "revalidated"


def test_a_failed_filters_clear_is_ignored_and_clear_all_recovers() -> None:
    """`filters` are never fresh; an unscoped or foreign prefix flags everything until clear()."""
    runner, _, store, layer, _ = sync_setup()
    runner.send(make_request())
    store.ok = False
    layer.clear("v1/heroes/filters/")
    assert status(runner.send(make_request())) == "hit"
    layer.clear("v1/heroes/")  # names the resource without a kind
    assert status(runner.send(make_request())) == "revalidated"
    store.ok = True
    layer.clear("v1/heroes/list/")  # a successful partial clear recovers nothing
    assert status(runner.send(make_request())) == "miss"  # the entry is gone,
    assert status(runner.send(make_request())) == "revalidated"  # but not fresh
    store.ok = False
    layer.clear()
    store.ok = True
    layer.clear()  # everything cleared: all flags end
    runner.send(make_request())
    assert status(runner.send(make_request())) == "hit"
    store.ok = False
    layer.clear("not-a-cache-key")
    assert status(runner.send(make_request())) == "revalidated"


def test_a_failed_write_clear_and_the_per_prefix_fallback() -> None:
    """A write's clear that fails flags the resource; stores without clear_many are covered."""
    clock = Clock()
    flaky = Flaky(clock, many=False)
    layer = CacheLayer(flaky, {"heroes": POLICY}, clock=clock)
    layer.set_events_active("heroes", True)
    runner = SyncRunner(layer, Server())
    runner.send(make_request())
    assert status(runner.send(make_request())) == "hit"
    flaky.ok = False
    runner.send(make_request("create", method="POST"))
    assert status(runner.send(make_request())) == "revalidated"
    flaky.ok = True
    layer.clear_resource("heroes")
    runner.send(make_request())
    assert status(runner.send(make_request())) == "hit"


class AsyncFlaky:
    """Async twin of `Flaky`."""

    def __init__(self, store: Flaky) -> None:
        """Wrap `store`."""
        self.flaky = store

    async def get(self, key: str) -> CacheEntry | None:
        """Delegate."""
        return self.flaky.get(key)

    async def set(self, key: str, entry: CacheEntry, ttl: float | None = None) -> None:
        """Delegate."""
        self.flaky.set(key, entry, ttl)

    async def delete(self, key: str) -> None:
        """Delegate."""
        self.flaky.delete(key)

    async def clear(self, prefix: str | None = None) -> bool | None:
        """Delegate."""
        return self.flaky.clear(prefix)

    async def clear_many(self, prefixes: Sequence[str]) -> bool | None:
        """Delegate."""
        return self.flaky.clear_many(prefixes)


@pytest.mark.asyncio
async def test_async_failed_clear_revalidates_and_blocks_swr_until_reset() -> None:
    """The async layer: a failed clear ends fresh hits and stale-while-revalidate."""
    clock = Clock()
    store = Flaky(clock)
    layer = AsyncCacheLayer(AsyncFlaky(store), {"heroes": POLICY}, clock=clock)
    layer.set_events_active("heroes", True)
    runner = AsyncRunner(layer, Server())
    layer.bind(runner.raw)
    await runner.send(make_request())
    assert status(await runner.send(make_request())) == "hit"
    clock.now += 61
    store.ok = False
    await layer.invalidate_event("heroes", "create", 1)
    assert status(await runner.send(make_request())) == "revalidated"  # no SWR, no fresh
    await layer.clear_resource("heroes")
    assert status(await runner.send(make_request())) == "revalidated"
    await layer.clear("v1/heroes/list/")
    store.ok = True
    await layer.clear("v1/heroes/list/")
    assert status(await runner.send(make_request())) == "miss"
    assert status(await runner.send(make_request())) == "revalidated"
    await layer.clear_resource("heroes")
    await runner.send(make_request())
    assert status(await runner.send(make_request())) == "hit"
    store.ok = False
    await layer.clear()
    store.ok = True
    assert status(await runner.send(make_request())) == "revalidated"
    await layer.clear()
    await runner.send(make_request())
    assert status(await runner.send(make_request())) == "hit"
    await layer.aclose()
