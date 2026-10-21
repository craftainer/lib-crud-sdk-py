"""The shared cache scenarios through the real async client and the real async cache layer."""

import asyncio
import logging

import fakeredis.aioredis
import pytest
import redis

from craftainer_crud_sdk import (
    AsyncCacheLayer,
    AsyncCrudClient,
    AsyncMemoryStore,
    CachePolicy,
    PreconditionFailed,
    UnknownResourceError,
    aenable_cache,
    build_key,
    cache_scope,
    static_token,
)
from craftainer_crud_sdk.cache.redis import AsyncRedisStore

from ._fake_server import FakeServer, await_until
from ._support import BASE

pytestmark = pytest.mark.asyncio

FRESH = {"heroes": CachePolicy(max_age=60)}
SCOPE = cache_scope("tok")


def make_client(server: FakeServer, *, token: bool = True) -> AsyncCrudClient:
    """A client over the fake server (bearer token `tok` unless `token` is false)."""
    return AsyncCrudClient(
        BASE, transport=server.transport(), token_provider=static_token("tok") if token else None
    )


async def events_up(layer: AsyncCacheLayer) -> None:
    """Wait for the heroes stream to be connected."""
    await await_until(lambda: layer.events_active("heroes"))


async def test_304_revalidation_serves_the_stored_body() -> None:
    """The second list sends If-None-Match, gets a 304 and still returns the records."""
    server = FakeServer()
    async with make_client(server) as client:
        await aenable_cache(client, AsyncMemoryStore(), events=False)
        heroes = await client.resource("heroes")
        first = await heroes.list(sort="name")
        second = await heroes.list(sort="name")
    assert first == second == [server.rows[1], server.rows[2]]
    assert [status for *_, status in server.api("GET")] == [200, 304]
    assert server.requests[-1].headers["if-none-match"] == server.list_etag()


async def test_fresh_hit_needs_the_event_stream() -> None:
    """With max_age and the SSE stream up a read makes no request; without the stream it does."""
    server = FakeServer()
    async with make_client(server) as client:
        layer = await aenable_cache(client, AsyncMemoryStore(), overrides=FRESH)
        await events_up(layer)
        heroes = await client.resource("heroes")
        assert await heroes.list() == await heroes.list()
        assert await heroes.get(1) == await heroes.get(1)
    assert [status for *_, status in server.api("GET")] == [200, 200]

    quiet = FakeServer()
    async with make_client(quiet) as client:
        await aenable_cache(client, AsyncMemoryStore(), overrides=FRESH, events=False)
        heroes = await client.resource("heroes")
        await heroes.list()
        await heroes.list()
    assert [status for *_, status in quiet.api("GET")] == [200, 304]


async def test_sse_event_invalidates_the_cached_reads() -> None:
    """An update event from another client makes the next fresh read go to the network."""
    server = FakeServer()
    async with make_client(server) as client:
        layer = await aenable_cache(client, AsyncMemoryStore(), overrides=FRESH)
        await events_up(layer)
        heroes = await client.resource("heroes")
        assert (await heroes.list())[0]["name"] == "Ada"
        assert (await heroes.get(1))["name"] == "Ada"
        assert len(server.api("GET")) == 2
        server.external_update(1, name="Grace")
        while (await heroes.get(1))["name"] != "Grace":
            await asyncio.sleep(0.005)
        assert (await heroes.list())[0]["name"] == "Grace"
    assert len(server.api("GET")) >= 4


async def test_event_stream_is_authenticated_and_absolute() -> None:
    """The SSE request goes to the manifest events path with the client's bearer token."""
    server = FakeServer()
    async with make_client(server) as client:
        layer = await aenable_cache(client, AsyncMemoryStore())
        await events_up(layer)
    (sse,) = server.sse_requests()
    assert sse.headers["authorization"] == "Bearer tok"
    assert str(sse.url).startswith(BASE)


async def test_write_through_after_patch() -> None:
    """A PATCH response becomes the record entry; the list entry is cleared."""
    server = FakeServer()
    store = AsyncMemoryStore()
    async with make_client(server) as client:
        await aenable_cache(client, store, events=False)
        heroes = await client.resource("heroes")
        await heroes.get(1)
        await heroes.list()
        patched = await heroes.update(1, {"name": "Grace"})
        assert await store.get(build_key("heroes", "record", 1, SCOPE)) is not None
        assert await heroes.get(1) == patched  # 304 against the write-through ETag
        assert (await heroes.list())[0]["name"] == "Grace"  # list entry cleared: a full 200
    assert [(m, s) for m, _, s in server.api()] == [
        ("GET", 200), ("GET", 200), ("PATCH", 200), ("GET", 304), ("GET", 200),
    ]  # fmt: skip


async def test_412_on_stale_if_match_with_auto_if_match() -> None:
    """auto_if_match sends the cached ETag; a stale one is a 412 that evicts the record."""
    server = FakeServer()
    store = AsyncMemoryStore()
    async with make_client(server) as client:
        await aenable_cache(client, store, events=False, auto_if_match=True)
        heroes = await client.resource("heroes")
        await heroes.get(1)
        await heroes.update(1, {"name": "Grace"})  # the cached ETag is current: accepted
        server.change(1, name="Elsewhere")  # somebody else wins; no event reaches us
        with pytest.raises(PreconditionFailed):
            await heroes.update(1, {"name": "Lost"})
        assert server.requests[-1].headers["if-match"] == '"r1-2"'
        assert await store.get(build_key("heroes", "record", 1, SCOPE)) is None
        assert (await heroes.get(1))["name"] == "Elsewhere"
        await heroes.update(1, {"name": "Won"}, if_match=server.etag(1))  # explicit wins
        await heroes.update(2, {"name": "Blind"})  # nothing cached for 2: no If-Match
    assert "if-match" not in server.requests[-1].headers


async def test_auto_if_match_with_an_async_provider_and_without_a_token() -> None:
    """The lookup scope comes from an async provider, or is anon with no provider."""

    async def provider(*, refresh: bool = False) -> str | None:
        return "tok"

    server = FakeServer()
    async with AsyncCrudClient(BASE, transport=server.transport(), token_provider=provider) as c:
        await aenable_cache(c, AsyncMemoryStore(), events=False, auto_if_match=True)
        heroes = await c.resource("heroes")
        await heroes.get(1)
        await heroes.update(1, {"name": "Grace"})
    assert server.requests[-1].headers["if-match"] == '"r1-1"'

    anon = FakeServer()
    async with make_client(anon, token=False) as client:
        await aenable_cache(client, AsyncMemoryStore(), events=False, auto_if_match=True)
        heroes = await client.resource("heroes")
        await heroes.get(1)
        await heroes.update(1, {"name": "Grace"})
    assert anon.requests[-1].headers["if-match"] == '"r1-1"'


async def test_no_store_is_never_stored() -> None:
    """A no-store response is not stored, so nothing is revalidated."""
    server = FakeServer()
    server.no_store = True
    store = AsyncMemoryStore()
    async with make_client(server) as client:
        await aenable_cache(client, store, events=False)
        heroes = await client.resource("heroes")
        await heroes.list()
        await heroes.list()
        await heroes.get(1)
    assert [status for *_, status in server.api()] == [200, 200, 200]
    assert all("if-none-match" not in r.headers for r in server.requests[2:])
    assert await store.get(build_key("heroes", "record", 1, SCOPE)) is None


async def test_redis_store_works_through_the_client() -> None:
    """The Redis store revalidates like the memory store."""
    server = FakeServer()
    async with make_client(server) as client:
        store = AsyncRedisStore(fakeredis.aioredis.FakeRedis())
        await aenable_cache(client, store, events=False)
        heroes = await client.resource("heroes")
        assert await heroes.list() == await heroes.list()
    assert [status for *_, status in server.api()] == [200, 304]


class Down(fakeredis.aioredis.FakeRedis):
    """A Redis client whose every command fails."""

    async def execute_command(self, *args: object, **options: object) -> object:
        """Fail like an outage."""
        raise redis.ConnectionError("down")


async def test_store_outage_is_a_miss(caplog: pytest.LogCaptureFixture) -> None:
    """With the store down every read is a plain request and a write still succeeds."""
    server = FakeServer()
    async with make_client(server) as client:
        with caplog.at_level(logging.WARNING):
            await aenable_cache(client, AsyncRedisStore(Down()), events=False, auto_if_match=True)
            heroes = await client.resource("heroes")
            assert len(await heroes.list()) == 2
            assert len(await heroes.list()) == 2
            assert (await heroes.get(1))["name"] == "Ada"
            assert (await heroes.update(1, {"name": "Grace"}))["name"] == "Grace"
    assert [status for *_, status in server.api()] == [200, 200, 200, 200]
    assert all("if-none-match" not in r.headers for r in server.requests)
    assert "redis" in caplog.text


async def test_selection_overrides_and_validation() -> None:
    """Only the selected resources are cached; bad names and a second cache are refused."""
    server = FakeServer()
    async with make_client(server) as client:
        with pytest.raises(UnknownResourceError):
            await aenable_cache(client, AsyncMemoryStore(), resources=["nope"])
        with pytest.raises(UnknownResourceError):
            await aenable_cache(
                client,
                AsyncMemoryStore(),
                resources=["heroes"],
                overrides={"quests": FRESH["heroes"]},
            )
        layer = await aenable_cache(client, AsyncMemoryStore(), resources=["heroes"], events=False)
        assert await layer.cached_etag("heroes", 1, SCOPE) is None
        assert client.hooks == (layer,)
        with pytest.raises(ValueError, match="already"):
            await aenable_cache(client, AsyncMemoryStore())


async def test_resources_without_an_event_stream_are_cached_by_revalidation() -> None:
    """The default selection covers every resource; quests has no stream and starts none."""
    server = FakeServer()
    async with make_client(server) as client:
        layer = await aenable_cache(client, AsyncMemoryStore())
        await events_up(layer)
        assert not layer.events_active("quests")
    assert len(server.feeds) == 1


async def test_close_stops_the_subscribers_and_the_layer_tasks() -> None:
    """Closing the client cancels the stream task and the layer's background work."""
    server = FakeServer()
    client = make_client(server)
    before = len(asyncio.all_tasks())
    layer = await aenable_cache(client, AsyncMemoryStore())
    await events_up(layer)
    assert len(asyncio.all_tasks()) == before + 1
    await client.aclose()
    assert not layer.events_active("heroes")
    assert len(asyncio.all_tasks()) == before
    await client.aclose()  # idempotent
