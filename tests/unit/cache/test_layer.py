"""The synchronous cache hook against the shared scenarios."""

import httpx2

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
    seed(store, "v1/heroes/filters/_/s1/")
    seed(store, "v1/villains/filters/_/s1/")
    seed(store, "v1/heroes/list/_/s1/")
    assert (
        status_of(runner.send(make_request("manifest", resource=None, scope="s1"))) == "revalidated"
    )
    assert len(store) == 4  # unchanged ETag: nothing cleared
    server.etag = '"m2"'
    runner.send(make_request("manifest", resource=None, scope="s1"))
    assert store.get("v1/heroes/filters/_/s1/") is None
    assert store.get("v1/villains/filters/_/s1/") is None
    assert store.get("v1/heroes/list/_/s1/") is not None
    assert store.get("v1/_manifest/s1") is not None
    assert layer.cached_etag("heroes", 1, "s1") is None


def test_manifest_with_unreadable_body_and_disabled() -> None:
    """Manifest with unreadable body and disabled."""
    runner, server, store, _, _ = setup()
    server.body = ["not", "a", "manifest"]
    runner.send(make_request("manifest", resource=None))
    server.etag = '"m2"'
    seed(store, "v1/heroes/filters/_/anon/")
    runner.send(make_request("manifest", resource=None))
    assert store.get("v1/heroes/filters/_/anon/") is None  # policies still name heroes
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
        "v1/heroes/filters/_/anon/",
        "v1/heroes/record/1/anon/",
        "v1/heroes/record/2/anon/",
        "v1/heroes/revisions/1/anon/",
        "v1/heroes/revisions/2/anon/",
    }
    for action in ("update", "delete", "restore"):
        fill()
        layer.invalidate_event("heroes", action, 1)
        assert alive() == {
            "v1/heroes/filters/_/anon/",
            "v1/heroes/record/2/anon/",
            "v1/heroes/revisions/2/anon/",
        }
    fill()
    layer.invalidate_event("heroes", "update", None)
    assert alive() == {"v1/heroes/filters/_/anon/"}
    fill()
    layer.clear_resource("heroes")
    assert alive() == {"v1/heroes/filters/_/anon/"}
    layer.clear("v1/heroes/")
    assert alive() == set()
    fill()
    layer.clear()
    assert alive() == set()
