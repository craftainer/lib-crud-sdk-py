"""The shared cache scenarios through the real sync client and the real cache layer."""

import logging
import threading

import fakeredis
import pytest
import redis

import craftainer_crud_sdk
from craftainer_crud_sdk import (
    CacheLayer,
    CachePolicy,
    CrudClient,
    MemoryStore,
    PreconditionFailed,
    UnknownResourceError,
    build_key,
    cache_scope,
    enable_cache,
    static_token,
)
from craftainer_crud_sdk.cache.redis import RedisStore

from ._fake_server import FakeServer, wait_until
from ._support import BASE

FRESH = {"heroes": CachePolicy(max_age=60)}
SCOPE = cache_scope("tok")


def make_client(server: FakeServer, *, token: bool = True) -> CrudClient:
    """A client over the fake server (bearer token `tok` unless `token` is false)."""
    return CrudClient(
        BASE, transport=server.transport(), token_provider=static_token("tok") if token else None
    )


def events_up(layer: CacheLayer) -> None:
    """Wait for the heroes stream to be connected."""
    wait_until(lambda: layer.events_active("heroes"))


def test_root_exports_the_cache_api_but_not_redis() -> None:
    """Root exports the cache API but not redis."""
    for name in (
        "CachePolicy", "CacheLayer", "AsyncCacheLayer", "MemoryStore", "AsyncMemoryStore",
        "CACHE_STATUS_EXTENSION", "build_key", "canonical_query", "manifest_key",
        "EventSubscriber", "AsyncEventSubscriber", "invalidate_on_events",
        "enable_cache", "aenable_cache", "CacheHints",
    ):  # fmt: skip
        assert name in craftainer_crud_sdk.__all__
        assert hasattr(craftainer_crud_sdk, name)
    assert not hasattr(craftainer_crud_sdk, "RedisStore")


def test_304_revalidation_serves_the_stored_body() -> None:
    """The second list sends If-None-Match, gets a 304 and still returns the records."""
    server = FakeServer()
    with make_client(server) as client:
        enable_cache(client, MemoryStore(), events=False)
        heroes = client.resource("heroes")
        first = heroes.list(sort="name")
        second = heroes.list(sort="name")
    assert first == second == [server.rows[1], server.rows[2]]
    assert [status for *_, status in server.api("GET")] == [200, 304]
    assert server.requests[-1].headers["if-none-match"] == server.list_etag()


def test_fresh_hit_needs_the_event_stream() -> None:
    """With max_age and the SSE stream up a read makes no request; without the stream it does."""
    server = FakeServer()
    with make_client(server) as client:
        layer = enable_cache(client, MemoryStore(), overrides=FRESH)
        events_up(layer)
        heroes = client.resource("heroes")
        assert heroes.list() == heroes.list()
        assert heroes.get(1) == heroes.get(1)
    assert [status for *_, status in server.api("GET")] == [200, 200]

    quiet = FakeServer()
    with make_client(quiet) as client:
        enable_cache(client, MemoryStore(), overrides=FRESH, events=False)
        heroes = client.resource("heroes")
        heroes.list()
        heroes.list()
    assert [status for *_, status in quiet.api("GET")] == [200, 304]


def test_sse_event_invalidates_the_cached_reads() -> None:
    """An update event from another client makes the next fresh read go to the network."""
    server = FakeServer()
    with make_client(server) as client:
        layer = enable_cache(client, MemoryStore(), overrides=FRESH)
        events_up(layer)
        heroes = client.resource("heroes")
        assert heroes.list()[0]["name"] == "Ada"
        assert heroes.get(1)["name"] == "Ada"
        assert len(server.api("GET")) == 2
        server.external_update(1, name="Grace")
        wait_until(lambda: heroes.get(1)["name"] == "Grace")
        assert heroes.list()[0]["name"] == "Grace"
    assert len(server.api("GET")) >= 4


def test_event_stream_is_authenticated_and_absolute() -> None:
    """The SSE request goes to the manifest events path with the client's bearer token."""
    server = FakeServer()
    with make_client(server) as client:
        layer = enable_cache(client, MemoryStore())
        events_up(layer)
    (sse,) = server.sse_requests()
    assert sse.headers["authorization"] == "Bearer tok"
    assert str(sse.url).startswith(BASE)


def test_write_through_after_patch() -> None:
    """A PATCH response becomes the record entry; the list entry is cleared."""
    server = FakeServer()
    store = MemoryStore()
    with make_client(server) as client:
        enable_cache(client, store, events=False)
        heroes = client.resource("heroes")
        heroes.get(1)
        heroes.list()
        patched = heroes.update(1, {"name": "Grace"})
        assert store.get(build_key("heroes", "record", 1, SCOPE, api_version="v2")) is not None
        assert heroes.get(1) == patched  # 304 against the write-through ETag
        assert heroes.list()[0]["name"] == "Grace"  # the list entry was cleared: a full 200
    assert [(m, s) for m, _, s in server.api()] == [
        ("GET", 200), ("GET", 200), ("PATCH", 200), ("GET", 304), ("GET", 200),
    ]  # fmt: skip


def test_412_on_stale_if_match_with_auto_if_match() -> None:
    """auto_if_match sends the cached ETag; a stale one is a 412 that evicts the record."""
    server = FakeServer()
    store = MemoryStore()
    with make_client(server) as client:
        enable_cache(client, store, events=False, auto_if_match=True)
        heroes = client.resource("heroes")
        heroes.get(1)
        heroes.update(1, {"name": "Grace"})  # the cached ETag is current: accepted
        server.change(1, name="Elsewhere")  # somebody else wins; no event reaches us
        with pytest.raises(PreconditionFailed):
            heroes.update(1, {"name": "Lost"})
        assert server.requests[-1].headers["if-match"] == '"r1-2"'
        assert store.get(build_key("heroes", "record", 1, SCOPE, api_version="v2")) is None
        assert heroes.get(1)["name"] == "Elsewhere"
        heroes.update(1, {"name": "Won"}, if_match=server.etag(1))  # explicit wins, no lookup
        heroes.update(2, {"name": "Blind"})  # nothing cached for 2: no If-Match
    assert "if-match" not in server.requests[-1].headers


def test_auto_if_match_without_a_token_uses_the_anonymous_scope() -> None:
    """With no token provider the lookup runs under the anon scope."""
    server = FakeServer()
    with make_client(server, token=False) as client:
        enable_cache(client, MemoryStore(), events=False, auto_if_match=True)
        heroes = client.resource("heroes")
        heroes.get(1)
        heroes.update(1, {"name": "Grace"})
    assert server.requests[-1].headers["if-match"] == '"r1-1"'


def test_no_store_is_never_stored() -> None:
    """A no-store response is not stored, so nothing is revalidated."""
    server = FakeServer()
    server.no_store = True
    store = MemoryStore()
    with make_client(server) as client:
        enable_cache(client, store, events=False)
        heroes = client.resource("heroes")
        heroes.list()
        heroes.list()
        heroes.get(1)
    assert [status for *_, status in server.api()] == [200, 200, 200]
    assert all("if-none-match" not in r.headers for r in server.requests[2:])
    assert store.get(build_key("heroes", "record", 1, SCOPE, api_version="v2")) is None


def test_redis_store_works_through_the_client() -> None:
    """The Redis store revalidates like the memory store."""
    server = FakeServer()
    with make_client(server) as client:
        enable_cache(client, RedisStore(fakeredis.FakeRedis()), events=False)
        heroes = client.resource("heroes")
        assert heroes.list() == heroes.list()
    assert [status for *_, status in server.api()] == [200, 304]


class Down(fakeredis.FakeRedis):
    """A Redis client whose every command fails."""

    def execute_command(self, *args: object, **options: object) -> object:
        """Fail like an outage."""
        raise redis.ConnectionError("down")


def test_store_outage_is_a_miss(caplog: pytest.LogCaptureFixture) -> None:
    """With the store down every read is a plain request and a write still succeeds."""
    server = FakeServer()
    with make_client(server) as client, caplog.at_level(logging.WARNING):
        enable_cache(client, RedisStore(Down()), events=False, auto_if_match=True)
        heroes = client.resource("heroes")
        assert len(heroes.list()) == 2
        assert len(heroes.list()) == 2
        assert heroes.get(1)["name"] == "Ada"
        assert heroes.update(1, {"name": "Grace"})["name"] == "Grace"
    assert [status for *_, status in server.api()] == [200, 200, 200, 200]
    assert all("if-none-match" not in r.headers for r in server.requests)
    assert "redis" in caplog.text


def test_selection_overrides_and_validation() -> None:
    """Only the selected resources are cached; bad names and a second cache are refused."""
    server = FakeServer()
    with make_client(server) as client:
        with pytest.raises(UnknownResourceError):
            enable_cache(client, MemoryStore(), resources=["nope"])
        with pytest.raises(UnknownResourceError):
            enable_cache(
                client, MemoryStore(), resources=["heroes"], overrides={"quests": FRESH["heroes"]}
            )
        layer = enable_cache(client, MemoryStore(), resources=["heroes"], events=False)
        assert layer.cached_etag("heroes", 1, SCOPE) is None
        assert client.hooks == (layer,)
        with pytest.raises(ValueError, match="already"):
            enable_cache(client, MemoryStore())


def test_resources_without_an_event_stream_are_cached_by_revalidation() -> None:
    """The default selection covers every resource; quests has no stream and starts none."""
    server = FakeServer()
    with make_client(server) as client:
        layer = enable_cache(client, MemoryStore())
        events_up(layer)
        assert not layer.events_active("quests")
    assert len(server.feeds) == 1


def test_close_stops_the_subscribers() -> None:
    """Closing the client ends the stream thread and closes the freshness gate."""
    server = FakeServer()
    client = make_client(server)
    before = threading.active_count()
    layer = enable_cache(client, MemoryStore())
    events_up(layer)
    assert threading.active_count() == before + 1
    client.close()
    assert not layer.events_active("heroes")
    assert threading.active_count() == before
    client.close()  # idempotent
