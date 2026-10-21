"""Memory and Redis stores."""

import logging
from unittest.mock import Mock

import fakeredis
import fakeredis.aioredis
import pytest
import redis
import redis.asyncio

from craftainer_crud_sdk.cache import AsyncMemoryStore, MemoryStore
from craftainer_crud_sdk.cache import redis as redis_store
from craftainer_crud_sdk.cache.redis import AsyncRedisStore, RedisStore
from craftainer_crud_sdk.interfaces import CacheEntry


def make_entry(tag: str = "a") -> CacheEntry:
    """Make entry."""
    return CacheEntry(200, (("etag", f'"{tag}"'),), tag.encode(), 1.0, 2.0, 3.0, f'"{tag}"')


class Clock:
    """Test helper."""

    def __init__(self) -> None:
        """Create it."""
        self.now = 0.0

    def __call__(self) -> float:
        """Protocol method."""
        return self.now


def test_memory_lru_eviction() -> None:
    """Memory lru eviction."""
    store = MemoryStore(max_entries=2)
    store.set("a", make_entry("a"))
    store.set("b", make_entry("b"))
    assert store.get("a") is not None  # a is now most recent
    store.set("c", make_entry("c"))
    assert store.get("b") is None
    assert store.get("a") is not None
    assert store.get("c") is not None
    assert len(store) == 2


def test_memory_ttl_and_default_ttl() -> None:
    """Memory ttl and default ttl."""
    clock = Clock()
    store = MemoryStore(default_ttl=100, clock=clock)
    store.set("short", make_entry(), ttl=5)
    store.set("default", make_entry())
    clock.now = 6
    assert store.get("short") is None
    assert store.get("default") is not None
    clock.now = 101
    assert store.get("default") is None
    assert store.get("missing") is None


def test_memory_delete_and_clear() -> None:
    """Memory delete and clear."""
    store = MemoryStore()
    for key in ("v1/r/list/_/anon/", "v1/r/list/_/x/", "v1/r/record/1/anon/", "v1/q/list/_/a/"):
        store.set(key, make_entry())
    store.delete("v1/q/list/_/a/")
    store.delete("absent")
    store.clear("v1/r/list/")
    assert len(store) == 1
    store.clear()
    assert len(store) == 0


def test_memory_rejects_zero_capacity() -> None:
    """Memory rejects zero capacity."""
    with pytest.raises(ValueError, match="at least 1"):
        MemoryStore(max_entries=0)


@pytest.mark.asyncio
async def test_async_memory_store() -> None:
    """Async memory store."""
    store = AsyncMemoryStore()
    await store.set("p/a", make_entry())
    await store.set("p/b", make_entry("b"), ttl=10)
    await store.set("q", make_entry())
    assert (await store.get("p/a")) == make_entry()
    await store.delete("p/a")
    assert await store.get("p/a") is None
    await store.clear("p/")
    assert await store.get("p/b") is None
    assert await store.get("q") is not None
    await store.clear()
    assert await store.get("q") is None


@pytest.mark.parametrize("decode", [False, True])
def test_redis_roundtrip_and_namespaced_clear(decode: bool) -> None:
    """Redis roundtrip and namespaced clear."""
    client = fakeredis.FakeRedis(decode_responses=decode)
    client.set("other:data", "keep")
    store = RedisStore(client, namespace="ns:")
    store.set("v1/r/list/_/anon/", make_entry("a"), ttl=30)
    store.set("v1/r/record/1/anon/", make_entry("b"))
    store.set("v1/q/list/_/anon/", make_entry("c"))
    assert store.get("v1/r/list/_/anon/") == make_entry("a")
    assert 0 < client.pttl("ns:v1/r/list/_/anon/") <= 30_000
    assert store.get("absent") is None
    store.clear("v1/r/")
    assert store.get("v1/r/list/_/anon/") is None
    assert store.get("v1/q/list/_/anon/") is not None
    store.delete("v1/q/list/_/anon/")
    assert store.get("v1/q/list/_/anon/") is None
    store.set("x", make_entry())
    store.clear()
    assert client.keys("ns:*") == []
    assert client.get("other:data") is not None


def test_redis_clear_escapes_glob_and_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Redis clear escapes glob and batches."""
    monkeypatch.setattr(redis_store, "_BATCH", 2)
    client = fakeredis.FakeRedis()
    store = RedisStore(client, namespace="n*:")
    for i in range(5):
        store.set(f"a[1]/{i}", make_entry())
    store.set("a[2]/0", make_entry())
    client.set("nX:a[1]/0", "other")
    store.clear("a[1]/")
    assert store.get("a[2]/0") is not None
    assert all(store.get(f"a[1]/{i}") is None for i in range(5))
    assert client.get("nX:a[1]/0") == b"other"
    for i in range(4):  # an exact multiple of the batch size
        store.set(f"b/{i}", make_entry())
    store.clear("b/")
    assert store.get("b/3") is None


def test_redis_corrupt_value_is_a_miss(caplog: pytest.LogCaptureFixture) -> None:
    """Redis corrupt value is a miss."""
    client = fakeredis.FakeRedis()
    client.set("craftainer-crud:k", b"garbage")
    with caplog.at_level(logging.WARNING):
        assert RedisStore(client).get("k") is None
    assert "corrupt" in caplog.text


class Failing(fakeredis.FakeRedis):
    """A client whose every command fails like an outage."""

    def execute_command(self, *args: object, **options: object) -> object:
        """Execute command."""
        raise redis.ConnectionError("down")


def test_redis_outage_is_a_miss_never_raises(caplog: pytest.LogCaptureFixture) -> None:
    """Redis outage is a miss never raises."""
    store = RedisStore(Failing())
    with caplog.at_level(logging.WARNING):
        assert store.get("k") is None
        store.set("k", make_entry())
        store.delete("k")
        store.clear("p/")
    assert caplog.text.count("redis") >= 4


@pytest.mark.asyncio
async def test_async_redis_roundtrip_and_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    """Async redis roundtrip and clear."""
    monkeypatch.setattr(redis_store, "_BATCH", 2)
    client = fakeredis.aioredis.FakeRedis()
    await client.set("other", "keep")
    store = AsyncRedisStore(client, namespace="ns:")
    for i in range(5):
        await store.set(f"v1/r/list/{i}", make_entry(str(i)), ttl=30)
    await store.set("v1/q/x", make_entry())
    assert (await store.get("v1/r/list/3")) == make_entry("3")
    assert await store.get("absent") is None
    await store.clear("v1/r/")
    assert await store.get("v1/r/list/3") is None
    for i in range(4):  # an exact multiple of the batch size
        await store.set(f"b/{i}", make_entry())
    await store.clear("b/")
    assert await store.get("b/3") is None
    assert await store.get("v1/q/x") is not None
    await store.delete("v1/q/x")
    await store.set("y", make_entry())
    await store.clear()
    assert await client.keys("ns:*") == []
    assert await client.get("other") == b"keep"
    await client.aclose()


class AsyncFailing(fakeredis.aioredis.FakeRedis):
    """Test helper."""

    async def execute_command(self, *args: object, **options: object) -> object:
        """Execute command."""
        raise redis.ConnectionError("down")


@pytest.mark.asyncio
async def test_async_redis_outage_and_corruption(caplog: pytest.LogCaptureFixture) -> None:
    """Async redis outage and corruption."""
    store = AsyncRedisStore(AsyncFailing())
    with caplog.at_level(logging.WARNING):
        assert await store.get("k") is None
        await store.set("k", make_entry())
        await store.delete("k")
        await store.clear("p/")
    assert caplog.text.count("redis") >= 4
    client = fakeredis.aioredis.FakeRedis()
    await client.set("craftainer-crud:k", b"garbage")
    with caplog.at_level(logging.WARNING):
        assert await AsyncRedisStore(client).get("k") is None
    assert "corrupt" in caplog.text


def test_memory_clear_many_is_one_pass_over_all_prefixes() -> None:
    """Memory clear many removes every key under any prefix and keeps the rest."""
    store = MemoryStore()
    for key in ("a/1", "a/2", "b/1", "c/1"):
        store.set(key, make_entry())
    store.clear_many([])
    assert len(store) == 4
    store.clear_many(["a/", "b/"])
    assert store.get("a/1") is None
    assert store.get("b/1") is None
    assert store.get("c/1") is not None


@pytest.mark.asyncio
async def test_async_memory_clear_many() -> None:
    """Async memory clear many delegates."""
    store = AsyncMemoryStore()
    await store.set("a/1", make_entry())
    await store.set("c/1", make_entry())
    await store.clear_many(["a/", "z/"])
    assert await store.get("a/1") is None
    assert await store.get("c/1") is not None


@pytest.mark.parametrize("decode", [False, True])
def test_redis_clear_many_scans_once(decode: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redis clear many: one SCAN pass, every prefix tested, other keys untouched."""
    monkeypatch.setattr(redis_store, "_BATCH", 1000)
    prefixes = ["v1/r/list/", "v1/r/stats/", "v1/r/record/1/"]
    keys = ("v1/r/list/1", "v1/r/list/2", "v1/r/stats/1", "v1/r/record/1/x", "v1/r/filters/1")
    client = fakeredis.FakeRedis(decode_responses=decode)
    scan = Mock(wraps=client.scan)
    monkeypatch.setattr(client, "scan", scan)
    client.set("other:v1/r/list/x", "keep")
    store = RedisStore(client, namespace="ns:")
    for key in keys:
        store.set(key, make_entry())
    store.clear_many([])  # nothing to do
    scan.assert_not_called()
    store.clear_many(prefixes)
    one_pass = scan.call_count
    assert store.get("v1/r/list/1") is None
    assert store.get("v1/r/list/2") is None
    assert store.get("v1/r/stats/1") is None
    assert store.get("v1/r/record/1/x") is None
    assert store.get("v1/r/filters/1") is not None
    assert client.get("other:v1/r/list/x") is not None
    for key in keys:
        store.set(key, make_entry())
    scan.reset_mock()
    for prefix in prefixes:
        store.clear(prefix)
    assert one_pass * len(prefixes) <= scan.call_count


def test_redis_clear_many_outage_is_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    """Redis clear many never raises on an outage."""
    store = RedisStore(Failing())
    with caplog.at_level(logging.WARNING):
        store.clear_many(["p/", "q/"])
    assert "redis clear failed" in caplog.text


@pytest.mark.asyncio
async def test_async_redis_clear_many(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Async redis clear many: batches, one pass, outage is a warning."""
    monkeypatch.setattr(redis_store, "_BATCH", 2)
    client = fakeredis.aioredis.FakeRedis()
    await client.set("other:v1/r/list/x", "keep")
    store = AsyncRedisStore(client, namespace="ns:")
    for i in range(5):
        await store.set(f"v1/r/list/{i}", make_entry())
    await store.set("v1/r/stats/1", make_entry())
    await store.set("v1/r/filters/1", make_entry())
    await store.clear_many([])
    assert await store.get("v1/r/list/0") is not None
    await store.clear_many(["v1/r/list/", "v1/r/stats/"])
    assert await store.get("v1/r/list/4") is None
    assert await store.get("v1/r/stats/1") is None
    assert await store.get("v1/r/filters/1") is not None
    assert await client.get("other:v1/r/list/x") == b"keep"
    await client.aclose()
    with caplog.at_level(logging.WARNING):
        await AsyncRedisStore(AsyncFailing()).clear_many(["p/"])
    assert "redis clear failed" in caplog.text
