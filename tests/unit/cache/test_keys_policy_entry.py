"""Keys, policy and entry helpers."""

import httpx2
import pytest

from craftainer_crud_sdk.cache import CachePolicy, build_key, canonical_query, manifest_key
from craftainer_crud_sdk.cache.entry import (
    build_entry,
    cache_control,
    entry_from_json,
    entry_to_json,
    is_json,
    merge_headers,
    to_response,
    vary_allows_caching,
)
from craftainer_crud_sdk.cache.keys import (
    event_prefixes,
    kind_prefix,
    prefix_parts,
    record_prefix,
    resource_prefix,
    resource_prefixes,
    revisions_prefix,
    subject_for,
)
from craftainer_crud_sdk.interfaces import CacheEntry


def test_canonical_query_sorts_and_drops_id_but_keeps_values() -> None:
    """Canonical query sorts and drops id but keeps values."""
    a = canonical_query("limit=10&name__in=a,b&id=5&sort=-x&flag")
    b = canonical_query(b"flag&sort=-x&name__in=a,b&limit=10&")
    assert a == b == "flag&limit=10&name__in=a,b&sort=-x"
    assert canonical_query("q=a%20b&t=2026-10-20T10%3A00Z") == "q=a%20b&t=2026-10-20T10%3A00Z"
    assert canonical_query("") == ""
    assert canonical_query("id=7") == ""


def test_build_key_shapes() -> None:
    """Build key shapes."""
    assert build_key("heroes", "list", None, "anon", "limit=5") == "v1/heroes/list/_/_/anon/limit=5"
    assert build_key("heroes", "record", 7, "abc", "id=7") == "v1/heroes/record/7/_/abc/"
    assert build_key("heroes", "revisions", 7, "abc") == "v1/heroes/revisions/7/_/abc/"
    assert build_key("heroes", "stats", 7, "abc") == "v1/heroes/stats/_/_/abc/"
    assert build_key("heroes", "list", None, "a", api_version="v2") == "v1/heroes/list/_/v2/a/"
    assert subject_for("record", None) == "_"
    assert manifest_key("anon") == "v1/_manifest/anon"


def test_versions_do_not_share_keys_but_share_every_invalidation_prefix() -> None:
    """V1 and v2 get distinct keys, both under the same kind and record prefixes."""
    v1 = build_key("r", "record", 3, "s", "limit=5", api_version="v1")
    v2 = build_key("r", "record", 3, "s", "limit=5", api_version="v2")
    assert v1 != v2
    for prefix in (resource_prefix("r"), kind_prefix("r", "record"), record_prefix("r", 3)):
        assert v1.startswith(prefix) and v2.startswith(prefix)


def test_prefix_helpers() -> None:
    """Prefix helpers."""
    assert resource_prefix("r") == "v1/r/"
    assert kind_prefix("r", "list") == "v1/r/list/"
    assert record_prefix("r", 3) == "v1/r/record/3/"
    assert revisions_prefix("r", 3) == "v1/r/revisions/3/"
    assert "v1/r/filters/" not in resource_prefixes("r")
    assert len(resource_prefixes("r")) == 5


def test_event_prefix_table() -> None:
    """Event prefix table."""
    three = ["v1/r/list/", "v1/r/stats/", "v1/r/predict/"]
    assert event_prefixes("r", "create", 9) == three
    for action in ("update", "delete", "restore"):
        assert event_prefixes("r", action, 9) == [*three, "v1/r/record/9/", "v1/r/revisions/9/"]
    assert event_prefixes("r", "update", None) == resource_prefixes("r")
    assert event_prefixes("r", "weird", 9) == resource_prefixes("r")


def test_policy_from_manifest_defaults_and_overrides() -> None:
    """Policy from manifest defaults and overrides."""
    resource = {
        "cache": {"etag": False, "max_age": 5, "stats_ttl": None, "stale_while_revalidate": 7}
    }
    policy = CachePolicy.from_manifest(resource)
    assert (policy.etag, policy.max_age, policy.stats_ttl, policy.stale_while_revalidate) == (
        False,
        5.0,
        0.0,
        7.0,
    )
    over = CachePolicy.from_manifest(
        resource, enabled=False, max_age=1, stats_ttl=2, stale_while_revalidate=3
    )
    assert (over.enabled, over.max_age, over.stats_ttl, over.stale_while_revalidate) == (
        False,
        1,
        2,
        3,
    )
    assert policy.revalidate_only is False
    hinted = {"cache": {"revalidate_only": True}}
    assert CachePolicy.from_manifest(hinted).revalidate_only is True
    assert CachePolicy.from_manifest(hinted, revalidate_only=False).revalidate_only is False
    bare = CachePolicy.from_manifest({})
    assert bare == CachePolicy()
    assert CachePolicy.from_manifest({"cache": {"max_age": True}}).max_age == 0.0
    assert CachePolicy.from_manifest({"cache": {"max_age": -3}}).max_age == 0.0


def test_cache_control_and_content_helpers() -> None:
    """Cache control and content helpers."""
    headers = httpx2.Headers(
        [("cache-control", "Private, No-Cache"), ("cache-control", "max-age=3")]
    )
    assert cache_control(headers) == {"private", "no-cache", "max-age"}
    assert cache_control(httpx2.Headers()) == frozenset()
    assert is_json(httpx2.Response(200, json={}))
    assert is_json(httpx2.Response(200, headers={"content-type": "application/problem+json"}))
    assert not is_json(httpx2.Response(200, text="x"))
    assert vary_allows_caching(httpx2.Response(200, headers={"vary": "Accept, Authorization"}))
    assert vary_allows_caching(httpx2.Response(200))
    assert not vary_allows_caching(httpx2.Response(200, headers={"vary": "*"}))
    assert not vary_allows_caching(httpx2.Response(200, headers={"vary": "Cookie"}))


def test_entry_roundtrip_and_response_rebuild() -> None:
    """Entry roundtrip and response rebuild."""
    response = httpx2.Response(
        200, json={"a": 1}, headers={"ETag": '"x"', "Content-Encoding": "identity"}
    )
    entry = build_entry(response, now=10.0, fresh=5.0, stale=3.0, keep_etag=True)
    assert entry.etag == '"x"'
    assert (entry.fresh_until, entry.stale_until) == (15.0, 18.0)
    assert all(k.lower() != "content-encoding" for k, _ in entry.headers)
    assert entry_from_json(entry_to_json(entry)) == entry
    assert entry_from_json(entry_to_json(entry).decode()) == entry
    assert build_entry(response, now=0, fresh=0, stale=0, keep_etag=False).etag is None
    request = httpx2.Request("GET", "http://t/x")
    rebuilt = to_response(entry, request, status="hit", extensions={"k": 1})
    assert rebuilt.json() == {"a": 1}
    assert rebuilt.extensions["craftainer_cache"] == "hit"
    assert rebuilt.extensions["k"] == 1


def test_entry_from_json_rejects_corrupt() -> None:
    """Entry from json rejects corrupt."""
    with pytest.raises(ValueError, match="Expecting"):
        entry_from_json("not json")
    with pytest.raises(KeyError):
        entry_from_json("{}")


def test_merge_headers_replaces_by_name() -> None:
    """Merge headers replaces by name."""
    stored = (("ETag", '"a"'), ("Content-Type", "application/json"), ("X-Old", "1"))
    update = httpx2.Headers([("etag", '"b"'), ("content-length", "0"), ("X-New", "2")])
    merged = dict(merge_headers(stored, update))
    assert merged == {"Content-Type": "application/json", "X-Old": "1", "etag": '"b"', "x-new": "2"}


def test_cache_entry_default_etag_none() -> None:
    """Cache entry default etag none."""
    assert CacheEntry(200, (), b"", 0.0, 0.0, 0.0).etag is None


def test_prefix_parts() -> None:
    """The (resource, kind) a prefix names; None when it names no resource."""
    assert prefix_parts("v1/heroes/list/") == ("heroes", "list")
    assert prefix_parts("v1/heroes/") == ("heroes", None)
    assert prefix_parts("v1/heroes") == ("heroes", None)
    for none in (None, "", "v1/", "v2/heroes/", "heroes/list/", "v1//list/"):
        assert prefix_parts(none) is None
