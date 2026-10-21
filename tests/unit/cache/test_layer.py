"""The synchronous cache hook against the shared scenarios."""

from collections.abc import Sequence

import httpx2
import pytest

from craftainer_crud_sdk.cache import CacheLayer, CachePolicy, MemoryStore, build_key
from craftainer_crud_sdk.interfaces import CacheEntry, Op
from tests.unit.cache.harness import ETAG, Clock, Server, SyncRunner, make_request

POLICY = CachePolicy(max_age=60, stats_ttl=30, stale_while_revalidate=120)


def setup(
    policy: CachePolicy = POLICY, *, events: bool = False, manifest: bool = True
) -> tuple[SyncRunner, Server, MemoryStore, CacheLayer, Clock]:
    """Setup."""
    clock = Clock()
    store = MemoryStore(clock=clock)
    layer = CacheLayer(store, {"heroes": policy}, cache_manifest=manifest, clock=clock)
    if events:
        layer.set_events_active("heroes", True)
    server = Server()
    return SyncRunner(layer, server), server, store, layer, clock


def status_of(response: httpx2.Response) -> object:
    """Status of."""
    return response.extensions.get("craftainer_cache")


def seed(store: MemoryStore, key: str, *, etag: str | None = ETAG) -> None:
    """Seed."""
    store.set(key, CacheEntry(200, (), b"{}", 0.0, 0.0, 0.0, etag))


def test_fresh_hit_needs_events_and_expires() -> None:
    """Fresh hit needs events and expires."""
    runner, server, _, layer, clock = setup(events=True)
    first = runner.send(make_request())
    assert status_of(first) == "miss"
    assert first.json() == {"id": 1, "name": "a"}
    clock.now += 59
    hit = runner.send(make_request())
    assert status_of(hit) == "hit"
    assert hit.json() == {"id": 1, "name": "a"}
    assert len(server.calls) == 1
    layer.set_events_active("heroes", False)  # stream lost: revalidate instead
    assert not layer.events_active("heroes")
    assert status_of(runner.send(make_request())) == "revalidated"
    layer.set_events_active("heroes", True)
    clock.now += 61
    assert status_of(runner.send(make_request())) == "revalidated"  # past max_age
    assert status_of(runner.send(make_request())) == "hit"


def test_entries_stored_before_the_stream_never_become_fresh() -> None:
    """Entries stored before the stream never become fresh."""
    runner, server, _, layer, _ = setup()
    runner.send(make_request())
    layer.set_events_active("heroes", True)
    assert status_of(runner.send(make_request())) == "revalidated"
    assert len(server.calls) == 2


def test_revalidation_304_merges_headers() -> None:
    """Revalidation 304 merges headers."""
    runner, server, _, _, _ = setup()
    server.headers = {"X-Keep": "k"}
    runner.send(make_request())
    response = runner.send(make_request())
    assert status_of(response) == "revalidated"
    assert response.status_code == 200
    assert response.json() == {"id": 1, "name": "a"}
    assert response.headers["x-new"] == "1"
    assert response.headers["x-keep"] == "k"
    assert response.headers["content-type"] == "application/json"
    assert server.calls[1].headers["if-none-match"] == ETAG
    # a changed representation replaces the entry
    server.etag = '"v2"'
    server.body = {"id": 1, "name": "b"}
    assert runner.send(make_request()).json() == {"id": 1, "name": "b"}
    assert runner.send(make_request()).json() == {"id": 1, "name": "b"}


def test_304_updates_etag_and_policy_without_etag() -> None:
    """304 updates etag and policy without etag."""
    runner, server, store, _, _ = setup(CachePolicy(etag=False, max_age=10))
    runner.send(make_request())
    assert len(store) == 0  # no etag kept and never fresh: nothing to store
    assert "if-none-match" not in server.calls[0].headers


def test_etag_disabled_but_fresh_entry_is_stored_without_validator() -> None:
    """Etag disabled but fresh entry is stored without validator."""
    runner, server, _, layer, clock = setup(CachePolicy(etag=False, max_age=10), events=True)
    runner.send(make_request())
    assert status_of(runner.send(make_request())) == "hit"
    clock.now += 11
    assert status_of(runner.send(make_request())) == "miss"  # no validator: plain fetch
    assert "if-none-match" not in server.calls[-1].headers
    layer.set_events_active("heroes", False)
    assert status_of(runner.send(make_request())) == "miss"


def test_no_store_response_never_stored() -> None:
    """No store response never stored."""
    runner, server, store, _, _ = setup()
    server.headers = {"Cache-Control": "no-store"}
    runner.send(make_request())
    assert len(store) == 0


def test_unstorable_responses_are_not_stored() -> None:
    """Unstorable responses are not stored."""
    runner, server, store, _, _ = setup()
    server.headers = {"Vary": "*"}
    runner.send(make_request())
    server.headers = {"Vary": "Cookie"}
    runner.send(make_request())
    assert len(store) == 0
    plain = httpx2.MockTransport(lambda r: httpx2.Response(200, text="hi", headers={"ETag": ETAG}))
    layer = CacheLayer(store, {"heroes": POLICY})
    request = make_request()
    layer.on_request(request)
    layer.on_response(request, plain.handle_request(request))
    assert len(store) == 0


def test_request_no_store_no_cache_and_caller_validators() -> None:
    """Request no store no cache and caller validators."""
    runner, server, store, _, _ = setup(events=True)
    runner.send(make_request(headers={"Cache-Control": "no-store"}))
    assert len(store) == 0
    runner.send(make_request())
    assert status_of(runner.send(make_request())) == "hit"
    assert (
        status_of(runner.send(make_request(headers={"Cache-Control": "no-cache"}))) == "revalidated"
    )
    runner.send(make_request(headers={"If-None-Match": '"zzz"'}))
    assert server.calls[-1].headers["if-none-match"] == '"zzz"'
    assert len(store) == 1


def test_force_revalidate_without_etag_is_a_miss() -> None:
    """Force revalidate without etag is a miss."""
    runner, server, store, _, _ = setup(events=True)
    key = build_key("heroes", "list", None, "anon")
    seed(store, key, etag=None)
    runner.send(make_request(headers={"Cache-Control": "no-cache"}))
    assert "if-none-match" not in server.calls[0].headers


def test_retry_with_own_validator_keeps_caching() -> None:
    """Retry with own validator keeps caching."""
    runner, server, _, layer, _ = setup()
    runner.send(make_request())
    request = make_request()
    assert layer.on_request(request) is None
    assert request.headers["if-none-match"] == ETAG
    assert layer.on_request(request) is None  # retried by the client: still ours
    assert request.headers["if-none-match"] == ETAG
    response = layer.on_response(request, httpx2.Response(401))
    assert response.status_code == 401
    assert len(server.calls) == 1


def test_stats_use_stats_ttl_without_events() -> None:
    """Stats use stats ttl without events."""
    runner, server, _, _, clock = setup()
    runner.send(make_request("stats"))
    clock.now += 29
    assert status_of(runner.send(make_request("stats"))) == "hit"
    clock.now += 2
    assert status_of(runner.send(make_request("stats"))) == "revalidated"
    assert len(server.calls) == 2


def test_scopes_and_queries_are_separate_entries() -> None:
    """Scopes and queries are separate entries."""
    runner, server, store, _, _ = setup(events=True)
    runner.send(make_request(query="limit=5", scope="a"))
    runner.send(make_request(query="limit=5", scope="b"))
    runner.send(make_request(query="limit=6", scope="a"))
    assert len(server.calls) == 3
    assert status_of(runner.send(make_request(query="limit=5", scope="a"))) == "hit"
    assert len(store) == 3


def test_sync_layer_ignores_stale_window() -> None:
    """Sync layer ignores stale window."""
    runner, _, _, _, clock = setup(events=True)
    runner.send(make_request())
    clock.now += 100  # past fresh, inside the 120s SWR window
    assert status_of(runner.send(make_request())) == "revalidated"


def test_not_cached_paths_pass_through() -> None:
    """Not cached paths pass through."""
    runner, server, store, layer, _ = setup()
    runner.send(make_request(context=False))
    runner.send(make_request(resource="other"))
    runner.send(make_request("manifest", resource=None, method="GET"))  # cached, below
    runner.send(make_request("create", method="POST", resource="other"))
    runner.send(make_request("create", method="POST", resource=None))
    runner.send(make_request("events"))
    assert len(server.calls) == 6
    assert len(store) == 1  # only the manifest
    off = CacheLayer(store, {"heroes": CachePolicy(enabled=False)})
    request = make_request()
    assert off.on_request(request) is None
    assert layer.on_response(make_request(), httpx2.Response(200)).status_code == 200


def test_not_modified_without_our_validator_passes_through() -> None:
    """Not modified without our validator passes through."""
    _, _, _, layer, _ = setup()
    request = make_request()
    layer.on_request(request)
    assert layer.on_response(request, httpx2.Response(304)).status_code == 304


def test_gone_and_changed_to_unstorable_evict() -> None:
    """Gone and changed to unstorable evict."""
    runner, server, store, _, _ = setup()
    runner.send(make_request())
    server.etag = None
    server.status = 404
    runner.send(make_request())
    assert len(store) == 0
    server.status = 200
    server.etag = ETAG
    runner.send(make_request())
    assert len(store) == 1
    server.headers = {"Cache-Control": "no-store"}
    server.etag = '"other"'
    runner.send(make_request())
    assert len(store) == 0
    server.status = 500
    server.headers = {}
    runner.send(make_request())
    assert len(store) == 0


def test_server_error_keeps_entry() -> None:
    """Server error keeps entry."""
    runner, server, store, _, _ = setup()
    runner.send(make_request())
    server.status = 500
    server.etag = '"x"'
    runner.send(make_request())
    assert len(store) == 1


def test_manifest_cached_by_scope_and_filters_cleared_on_change() -> None:
    """Manifest cached by scope and filters cleared on change."""
    runner, server, store, layer, _ = setup()
    server.body = {"resources": [{"name": "heroes"}, {"name": "villains"}]}
    manifest = make_request("manifest", resource=None, scope="s1")
    runner.send(manifest)
    seed(store, "v1/heroes/filters/_/_/s1/")
    seed(store, "v1/villains/filters/_/_/s1/")
    seed(store, "v1/heroes/list/_/_/s1/")
    assert (
        status_of(runner.send(make_request("manifest", resource=None, scope="s1"))) == "revalidated"
    )
    assert len(store) == 4  # unchanged ETag: nothing cleared
    server.etag = '"m2"'
    runner.send(make_request("manifest", resource=None, scope="s1"))
    assert store.get("v1/heroes/filters/_/_/s1/") is None
    assert store.get("v1/villains/filters/_/_/s1/") is None
    assert store.get("v1/heroes/list/_/_/s1/") is not None
    assert store.get("v1/_manifest/s1") is not None
    assert layer.cached_etag("heroes", 1, "s1") is None


def test_manifest_with_unreadable_body_and_disabled() -> None:
    """Manifest with unreadable body and disabled."""
    runner, server, store, _, _ = setup()
    server.body = ["not", "a", "manifest"]
    runner.send(make_request("manifest", resource=None))
    server.etag = '"m2"'
    seed(store, "v1/heroes/filters/_/_/anon/")
    runner.send(make_request("manifest", resource=None))
    assert store.get("v1/heroes/filters/_/_/anon/") is None  # policies still name heroes
    runner2, server2, store2, _, _ = setup(manifest=False)
    runner2.send(make_request("manifest", resource=None))
    assert len(store2) == 0
    assert len(server2.calls) == 1


def test_write_through_after_patch() -> None:
    """Write through after patch."""
    runner, server, store, layer, _ = setup(events=True)
    runner.send(make_request("record", record_id=7, query="id=7"))
    runner.send(make_request("list"))
    runner.send(make_request("stats"))
    runner.send(make_request("revisions", record_id=7))
    other = make_request("record", record_id=8, query="id=8")
    runner.send(other)
    server.body = {"id": 7, "name": "patched"}
    server.etag = '"v2"'
    patch = make_request("update", method="PATCH", record_id=7, query="id=7")
    assert runner.send(patch).status_code == 200
    assert store.get(build_key("heroes", "list", None, "anon")) is None
    assert store.get(build_key("heroes", "stats", None, "anon")) is None
    assert store.get(build_key("heroes", "revisions", 7, "anon")) is None
    assert store.get(build_key("heroes", "record", 8, "anon")) is not None
    assert layer.cached_etag("heroes", 7, "anon") == '"v2"'
    calls = len(server.calls)
    hit = runner.send(make_request("record", record_id=7, query="id=7"))
    assert status_of(hit) == "hit"
    assert hit.json() == {"id": 7, "name": "patched"}
    assert len(server.calls) == calls


def test_write_through_skipped_when_not_storable() -> None:
    """Write through skipped when not storable."""
    runner, server, store, _, _ = setup(events=True)
    runner.send(make_request("record", record_id=7, query="id=7"))
    server.headers = {"Cache-Control": "no-store"}
    runner.send(make_request("update", method="PATCH", record_id=7))
    assert len(store) == 0
    server.headers = {}
    server.etag = None
    server.status = 200
    runner2, server2, store2, _, _ = setup(CachePolicy(), events=True)
    server2.etag = None
    runner2.send(make_request("update", method="PATCH", record_id=7))
    assert len(store2) == 0  # no etag, never fresh


def test_412_evicts_the_record() -> None:
    """412 evicts the record."""
    runner, server, store, _, _ = setup(events=True)
    runner.send(make_request("record", record_id=7, query="id=7"))
    runner.send(make_request("list"))
    server.status = 412
    runner.send(make_request("update", method="PATCH", record_id=7))
    assert store.get(build_key("heroes", "record", 7, "anon")) is None
    assert store.get(build_key("heroes", "list", None, "anon")) is not None
    server.status = 412
    runner.send(make_request("update", method="PATCH", record_id=None))  # nothing to evict
    assert store.get(build_key("heroes", "list", None, "anon")) is not None


def test_other_write_failures_change_nothing() -> None:
    """Other write failures change nothing."""
    runner, server, store, _, _ = setup(events=True)
    runner.send(make_request("list"))
    server.status = 422
    runner.send(make_request("create", method="POST"))
    assert len(store) == 1


def test_create_delete_publish_and_bulk_invalidation() -> None:
    """Create delete publish and bulk invalidation."""
    runner, server, store, _, _ = setup(events=True)

    ops_without_record: tuple[Op, ...] = ("list", "stats", "predict", "filters")

    def fill() -> None:
        """Fill."""
        for op in ops_without_record:
            runner.send(make_request(op))
        for rid in (1, 2, 3):
            runner.send(make_request("record", record_id=rid, query=f"id={rid}"))
            runner.send(make_request("revisions", record_id=rid))

    def has(kind: str, rid: int | None = None) -> bool:
        """Has."""
        return store.get(build_key("heroes", kind, rid, "anon")) is not None

    fill()
    server.status = 201
    runner.send(make_request("create", method="POST"))
    assert not has("list") and not has("stats") and not has("predict")
    assert has("filters") and has("record", 1)

    fill()
    server.status = 204
    runner.send(make_request("delete", method="DELETE", record_id=1))
    assert not has("list") and not has("record", 1) and not has("revisions", 1)
    assert has("record", 2)

    fill()
    server.status = 200
    runner.send(make_request("publish", method="POST", record_id=2))
    assert not has("record", 2) and has("record", 3)

    fill()
    server.body = {"matched": 2, "ids": [1, 3]}
    runner.send(make_request("bulk_update", method="PATCH"))
    assert not has("list") and not has("record", 1) and not has("revisions", 3)
    assert has("record", 2) and has("filters")

    fill()
    runner.send(make_request("restore", method="POST"))  # bulk restore
    assert not has("record", 3) and has("record", 2)


def test_bulk_with_unreadable_body_clears_the_resource_but_not_filters() -> None:
    """Bulk with unreadable body clears the resource but not filters."""
    runner, server, store, _, _ = setup(events=True)
    for op in ("list", "filters"):
        runner.send(make_request(op))
    runner.send(make_request("record", record_id=1, query="id=1"))
    server.body = {"matched": 1}
    runner.send(make_request("bulk_delete", method="DELETE"))
    assert len(store) == 1
    assert store.get(build_key("heroes", "filters", None, "anon")) is not None
    server.body = {"ids": ["x"]}
    runner.send(make_request("bulk_delete", method="DELETE"))


def test_unknown_write_op_clears_the_resource() -> None:
    """Unknown write op clears the resource."""
    runner, server, store, _, _ = setup(events=True)
    runner.send(make_request("list"))
    server.status = 200
    runner.send(make_request("events", method="POST"))
    assert len(store) == 0


def test_event_invalidation_per_action_row() -> None:
    """Event invalidation per action row."""
    runner, _, store, layer, _ = setup(events=True)

    def fill() -> None:
        """Fill."""
        for op in ("list", "stats", "predict", "filters"):
            runner.send(make_request(op))
        for rid in (1, 2):
            runner.send(make_request("record", record_id=rid, query=f"id={rid}"))
            runner.send(make_request("revisions", record_id=rid))

    def alive() -> set[str]:
        """Alive."""
        return set(store._data)

    fill()
    layer.invalidate_event("heroes", "create", 1)
    assert alive() == {
        "v1/heroes/filters/_/_/anon/",
        "v1/heroes/record/1/_/anon/",
        "v1/heroes/record/2/_/anon/",
        "v1/heroes/revisions/1/_/anon/",
        "v1/heroes/revisions/2/_/anon/",
    }
    for action in ("update", "delete", "restore"):
        fill()
        layer.invalidate_event("heroes", action, 1)
        assert alive() == {
            "v1/heroes/filters/_/_/anon/",
            "v1/heroes/record/2/_/anon/",
            "v1/heroes/revisions/2/_/anon/",
        }
    fill()
    layer.invalidate_event("heroes", "update", None)
    assert alive() == {"v1/heroes/filters/_/_/anon/"}
    fill()
    layer.clear_resource("heroes")
    assert alive() == {"v1/heroes/filters/_/_/anon/"}
    layer.clear("v1/heroes/")
    assert alive() == set()
    fill()
    layer.clear()
    assert alive() == set()


def test_events_clear_every_api_version_precisely() -> None:
    """An update of record 1 clears it in v1 and v2, and keeps record 2 and `filters`."""
    runner, _, store, layer, _ = setup(events=True)
    for version in ("v1", "v2"):
        for op in ("list", "filters"):
            runner.send(make_request(op, api_version=version))
        for rid in (1, 2):
            runner.send(
                make_request("record", record_id=rid, query=f"id={rid}", api_version=version)
            )
    layer.invalidate_event("heroes", "update", 1)
    assert set(store._data) == {
        "v1/heroes/filters/_/v1/anon/",
        "v1/heroes/filters/_/v2/anon/",
        "v1/heroes/record/2/v1/anon/",
        "v1/heroes/record/2/v2/anon/",
    }
    layer.clear_resource("heroes")
    assert set(store._data) == {"v1/heroes/filters/_/v1/anon/", "v1/heroes/filters/_/v2/anon/"}


def test_a_write_clears_the_other_versions_too() -> None:
    """A PATCH through v1 clears v2's list and its own record in v2."""
    runner, _, store, _, _ = setup(events=True)
    for version in ("v1", "v2"):
        runner.send(make_request("list", api_version=version))
        runner.send(make_request("record", record_id=1, query="id=1", api_version=version))
    runner.send(make_request("update", method="PATCH", record_id=1, api_version="v1"))
    assert set(store._data) == {"v1/heroes/record/1/v1/anon/"}  # only the write-through


def test_v1_and_v2_do_not_share_entries() -> None:
    """The same query under two API versions is two entries with their own ETags."""
    runner, server, store, layer, _ = setup(events=True)
    runner.send(make_request("record", record_id=7, query="id=7", api_version="v1"))
    server.etag = '"v2-etag"'
    server.body = {"id": 7, "name": "two"}
    second = runner.send(make_request("record", record_id=7, query="id=7", api_version="v2"))
    assert status_of(second) == "miss"  # not served from v1
    assert second.json()["name"] == "two"
    assert layer.cached_etag("heroes", 7, "anon", "v1") == ETAG
    assert layer.cached_etag("heroes", 7, "anon", "v2") == '"v2-etag"'
    assert layer.cached_etag("heroes", 7, "anon") is None  # unversioned
    assert store.get(build_key("heroes", "record", 7, "anon", api_version="v1")) is not None
    layer.invalidate_event("heroes", "update", 7)  # one event clears both versions
    assert len(store) == 0


def test_a_response_in_flight_during_an_invalidation_is_stored_revalidate_only() -> None:
    """Invalidate between request and response: the entry has no fresh window."""
    runner, server, store, layer, clock = setup(CachePolicy(max_age=60), events=True)
    request = make_request("list")
    assert layer.on_request(request) is None
    response = runner.transport.handle_request(request)
    layer.invalidate_event("heroes", "update", 1)  # lands while the response is in flight
    layer.on_response(request, response)
    entry = store.get(build_key("heroes", "list", None, "anon"))
    assert entry is not None
    assert entry.fresh_until <= entry.stored_at  # revalidate-only
    assert status_of(runner.send(make_request("list"))) == "revalidated"
    # a request made after the invalidation is stored fresh again
    store.clear()
    runner.send(make_request("list"))
    entry = store.get(build_key("heroes", "list", None, "anon"))
    assert entry is not None
    assert entry.fresh_until == clock.now + 60
    assert len(server.calls) == 3


def test_an_invalidation_during_the_store_drops_the_new_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An invalidation landing between the check and the store removes what was just stored."""
    runner, _, store, layer, _ = setup(CachePolicy(max_age=60), events=True)
    request = make_request("list")
    layer.on_request(request)
    response = runner.transport.handle_request(request)
    real_set = store.set

    def set_then_invalidate(key: str, entry: CacheEntry, ttl: float | None = None) -> None:
        """Store, then an event lands before the layer re-checks."""
        real_set(key, entry, ttl)
        layer.invalidate_event("heroes", "update", 1)

    monkeypatch.setattr(store, "set", set_then_invalidate)
    layer.on_response(request, response)
    assert len(store) == 0


def test_a_write_clears_reads_in_flight_too() -> None:
    """A read in flight while this client's own write completes is not stored fresh."""
    runner, server, store, layer, _ = setup(CachePolicy(max_age=60), events=True)
    read = make_request("list")
    layer.on_request(read)
    response = runner.transport.handle_request(read)
    server.status = 201
    runner.send(make_request("create", method="POST"))
    layer.on_response(read, response)
    entry = store.get(build_key("heroes", "list", None, "anon"))
    assert entry is not None
    assert entry.fresh_until <= entry.stored_at


def test_revalidate_only_policy_never_grants_freshness() -> None:
    """The manifest hint wins over max_age and stale_while_revalidate."""
    policy = CachePolicy(max_age=60, stale_while_revalidate=30, revalidate_only=True)
    runner, server, store, _, _ = setup(policy, events=True)
    runner.send(make_request("list"))
    entry = store.get(build_key("heroes", "list", None, "anon"))
    assert entry is not None
    assert entry.fresh_until <= entry.stored_at
    assert status_of(runner.send(make_request("list"))) == "revalidated"
    runner.send(make_request("stats"))
    stats = store.get(build_key("heroes", "stats", None, "anon"))
    assert stats is not None
    assert stats.fresh_until <= stats.stored_at
    assert len(server.calls) == 3


def test_a_storable_200_without_validator_or_freshness_deletes_the_old_entry() -> None:
    """No stale validator survives a 200 that cannot be revalidated."""
    runner, server, store, _, _ = setup()
    runner.send(make_request("list"))
    assert len(store) == 1
    server.etag = None
    server.status = 200
    runner.send(make_request("list"))
    assert len(store) == 0


class CountingStore(MemoryStore):
    """Counts how often the store is walked."""

    def __init__(self, **kwargs: object) -> None:
        """Create it."""
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.clears = 0
        self.many: list[list[str]] = []

    def clear(self, prefix: str | None = None) -> None:
        """Count."""
        self.clears += 1
        super().clear(prefix)

    def clear_many(self, prefixes: Sequence[str]) -> None:
        """Count."""
        self.many.append(list(prefixes))
        super().clear_many(prefixes)


class PlainStore:
    """A custom store that has only the four protocol methods."""

    def __init__(self) -> None:
        """Create it."""
        self.inner = MemoryStore()
        self.cleared: list[str | None] = []

    def get(self, key: str) -> CacheEntry | None:
        """Delegate."""
        return self.inner.get(key)

    def set(self, key: str, entry: CacheEntry, ttl: float | None = None) -> None:
        """Delegate."""
        self.inner.set(key, entry, ttl)

    def delete(self, key: str) -> None:
        """Delegate."""
        self.inner.delete(key)

    def clear(self, prefix: str | None = None) -> None:
        """Record, then delegate."""
        self.cleared.append(prefix)
        self.inner.clear(prefix)


def test_events_resets_and_bulk_writes_walk_the_store_once() -> None:
    """Each invalidation is one clear_many call, not a clear per prefix or per id."""
    store = CountingStore()
    layer = CacheLayer(store, {"heroes": POLICY})
    layer.invalidate_event("heroes", "update", 7)
    layer.clear_resource("heroes")
    assert len(store.many) == 2
    assert "v1/heroes/record/7/" in store.many[0]
    server = Server()
    runner = SyncRunner(layer, server)
    server.body = {"matched": 3, "ids": [1, 2, 3]}
    runner.send(make_request("bulk_update", method="PATCH"))
    assert len(store.many) == 3
    assert {f"v1/heroes/record/{i}/" for i in (1, 2, 3)} <= set(store.many[2])
    assert store.clears == 0


def test_a_store_without_clear_many_still_works() -> None:
    """Custom stores that only implement the protocol get a clear per prefix."""
    store = PlainStore()
    layer = CacheLayer(store, {"heroes": POLICY})
    layer.invalidate_event("heroes", "create", None)
    assert store.cleared == [
        "v1/heroes/list/", "v1/heroes/stats/", "v1/heroes/predict/",
    ]  # fmt: skip
