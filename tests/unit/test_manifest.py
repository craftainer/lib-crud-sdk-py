"""Tests for manifest parsing and discovery."""

import copy
from collections.abc import Callable
from typing import Any

import httpx2
import pytest

from craftainer_crud_sdk import (
    ConnectionFailed,
    Manifest,
    ManifestError,
    NotFound,
    aload_manifest,
    load_manifest,
    parse_manifest,
)
from craftainer_crud_sdk.manifest import manifest_url_from_catalog

from ._support import BASE, CASES, CATALOG, MANIFEST_PATH, PROFILE, load, load_doc, make_transport


@pytest.mark.parametrize("case", CASES)
def test_every_fixture_parses_to_the_same_resources(case: str) -> None:
    """Test every fixture parses to the same resources."""
    doc = load_doc(case)
    manifest = parse_manifest(doc)
    assert manifest.manifest_version == "1"
    assert (manifest.service_name, manifest.service_version) == ("heroes-api", "1.4.0")
    assert [(r.name, r.api_version) for r in manifest.resources] == [
        (r["name"], r["api_version"]) for r in doc["resources"]
    ]
    for parsed, raw in zip(manifest.resources, doc["resources"], strict=True):
        assert parsed.base_path == raw["base_path"]
        assert dict(parsed.paths) == raw["paths"]
        assert dict(parsed.formats) == raw["formats"]
        assert [f.name for f in parsed.fields] == [f["name"] for f in raw["fields"]]
        for field, raw_field in zip(parsed.fields, raw["fields"], strict=True):
            assert list(field.filter_ops) == raw_field["filter_ops"]
            assert field.sortable == raw_field["sortable"]
            assert field.value_type == raw_field["value_type"]
            assert (list(field.choices) if field.choices else None) == raw_field["choices"]
        assert parsed.pagination.max_limit == raw["pagination"]["max_limit"]
        assert parsed.bulk_max_matched == raw["bulk"]["max_matched"]
        assert parsed.cache.max_age == raw["cache"]["max_age"]
        assert parsed.cache.stats_ttl == raw["cache"]["stats_ttl"]
        assert parsed.cache.list_etag == raw["cache"]["list_etag"]
        assert list(parsed.auth.read_roles) == raw["auth"]["read_roles"]
        assert parsed.schemas.read == raw["schemas"]["read"]
        assert (parsed.schemas.draft is None) == (raw["schemas"]["draft"] is None)
        events = parsed.capabilities.events
        assert (events is None) == (raw["capabilities"]["events"] is None)
        if events is not None:
            assert events.path == raw["capabilities"]["events"]["path"] == parsed.paths["events"]


def test_admin_details() -> None:
    """Test admin details."""
    manifest = load()
    v2 = manifest.find("heroes", "v2")
    assert v2 is not None
    assert v2.capabilities.events is not None
    assert v2.capabilities.events.keepalive_seconds == 15.0
    assert v2.capabilities.events.resource == "hero"
    assert v2.schemas.draft is not None
    assert v2.field("created_at") is not None
    assert v2.field("nope") is None
    quests = manifest.find("quests")
    assert quests is not None
    assert quests.auth.public_read
    enum = quests.field("difficulty")
    assert enum is not None
    assert enum.choices == ("easy", "hard", "deadly")


def test_find_picks_highest_version_and_misses() -> None:
    """Test find picks highest version and misses."""
    manifest = load()
    latest = manifest.find("heroes")
    assert latest is not None
    assert latest.api_version == "v2"
    assert manifest.find("heroes", "v9") is None
    assert manifest.find("ghost") is None


def test_unknown_members_are_ignored() -> None:
    """Test unknown members are ignored."""
    doc = load_doc()
    doc["future"] = {"x": 1}
    doc["service"]["extra"] = 1
    resource = doc["resources"][0]
    resource["default_sort"] = "name"
    resource["fields"][0]["group"] = "main"
    resource["capabilities"]["new_flag"] = True
    resource["capabilities"]["events"]["new"] = 1
    resource["cache"]["new"] = 1
    resource["auth"]["new"] = 1
    resource["pagination"]["new"] = 1
    resource["bulk"]["new"] = 1
    assert parse_manifest(doc) == load()
    assert parse_manifest(_with_path(doc)).resources[0].paths["new_route"] == "/x"


def _with_path(doc: dict[str, Any]) -> dict[str, Any]:
    doc = copy.deepcopy(doc)
    doc["resources"][0]["paths"]["new_route"] = "/x"
    return doc


def _broken(mutate: Callable[[dict[str, Any]], object]) -> dict[str, Any]:
    doc = copy.deepcopy(load_doc())
    mutate(doc)
    return doc


BAD: list[Callable[[dict[str, Any]], object]] = [
    lambda d: d.pop("service"),
    lambda d: d["service"].pop("name"),
    lambda d: d.update(resources={}),
    lambda d: d.update(manifest_version="2"),
    lambda d: d.update(manifest_version=1),
    lambda d: d["resources"][0].pop("name"),
    lambda d: d["resources"][0].update(name=True),
    lambda d: d["resources"][0]["fields"][0].update(filter_ops=[1]),
    lambda d: d["resources"][0]["fields"][0].update(filter_ops="eq"),
    lambda d: d["resources"][0]["capabilities"].update(stats="yes"),
    lambda d: d["resources"][0]["capabilities"]["events"].update(keepalive_seconds="x"),
    lambda d: d["resources"][0]["pagination"].update(default_limit=True),
    lambda d: d["resources"][0].update(bulk=[]),
    lambda d: d["resources"][0]["auth"].update(oidc_discovery_url=5),
]


@pytest.mark.parametrize("mutate", BAD)
def test_malformed_documents_are_refused(mutate: Callable[[dict[str, Any]], object]) -> None:
    """Test malformed documents are refused."""
    with pytest.raises(ManifestError):
        parse_manifest(_broken(mutate))


def test_non_object_document() -> None:
    """Test non object document."""
    with pytest.raises(ManifestError, match="not a JSON object"):
        parse_manifest([])


def test_nullable_members() -> None:
    """Test nullable members."""
    doc = load_doc()
    resource = doc["resources"][0]
    resource["auth"]["oidc_discovery_url"] = None
    resource["cache"]["stats_ttl"] = None
    assert parse_manifest(doc).resources[0].auth.oidc_discovery_url is None


@pytest.mark.parametrize(
    "linkset",
    [
        [],
        {},
        {"linkset": "x"},
        {"linkset": [5]},
        {"linkset": [{"describedby": "x"}]},
        {"linkset": [{"describedby": [5, {"profile": "other", "href": "/m"}]}]},
        {"linkset": [{"describedby": [{"profile": PROFILE}]}]},
    ],
)
def test_catalog_without_manifest_link(linkset: object) -> None:
    """Test catalog without manifest link."""
    with pytest.raises(ManifestError, match="no describedby"):
        manifest_url_from_catalog(linkset, f"{BASE}/.well-known/api-catalog")


def test_catalog_href_is_resolved() -> None:
    """Test catalog href is resolved."""
    assert manifest_url_from_catalog(CATALOG, f"{BASE}/.well-known/api-catalog") == (
        f"{BASE}{MANIFEST_PATH}"
    )


def test_load_via_discovery_carries_context() -> None:
    """Test load via discovery carries context."""
    seen: list[httpx2.Request] = []
    with httpx2.Client(transport=make_transport(lambda r: httpx2.Response(404), seen=seen)) as http:
        manifest = load_manifest(http, f"{BASE}/api/")
    assert isinstance(manifest, Manifest)
    assert [r.url.path for r in seen] == ["/.well-known/api-catalog", MANIFEST_PATH]
    assert seen[0].headers["cache-control"] == "no-store"
    assert "cache-control" not in seen[1].headers
    for request in seen:
        assert request.extensions["craftainer_crud"] == {
            "resource": None, "op": "manifest", "record_id": None,
            "api_version": None, "scope": "anon",
        }  # fmt: skip


def test_load_direct_url_and_http_error() -> None:
    """Test load direct url and http error."""
    seen: list[httpx2.Request] = []
    with httpx2.Client(transport=make_transport(lambda r: httpx2.Response(404), seen=seen)) as http:
        load_manifest(http, BASE, manifest_url=f"{BASE}{MANIFEST_PATH}")
        assert len(seen) == 1
        with pytest.raises(NotFound):
            load_manifest(http, BASE, manifest_url=f"{BASE}/nope")


@pytest.mark.asyncio
async def test_async_load() -> None:
    """Test async load."""
    seen: list[httpx2.Request] = []
    async with httpx2.AsyncClient(
        transport=make_transport(lambda r: httpx2.Response(404), seen=seen)
    ) as http:
        assert (await aload_manifest(http, BASE)).find("quests") is not None
        assert len(seen) == 2
        await aload_manifest(http, BASE, manifest_url=f"{BASE}{MANIFEST_PATH}")
        with pytest.raises(NotFound):
            await aload_manifest(http, BASE, manifest_url=f"{BASE}/nope")


def _failing(error: Exception | None, body: str) -> httpx2.MockTransport:
    def handler(request: httpx2.Request) -> httpx2.Response:
        """Fail or answer with a non-JSON body."""
        if error is not None:
            raise error
        return httpx2.Response(200, text=body)

    return httpx2.MockTransport(handler)


def test_load_failures() -> None:
    """Transport errors and non-JSON bodies become package errors."""
    down = _failing(httpx2.ConnectError("down"), "")
    with httpx2.Client(transport=down) as http, pytest.raises(ConnectionFailed, match="down"):
        load_manifest(http, BASE)
    html = _failing(None, "<html>")
    with httpx2.Client(transport=html) as http, pytest.raises(ManifestError, match="not JSON"):
        load_manifest(http, BASE)


@pytest.mark.asyncio
async def test_async_load_failures() -> None:
    """Async twin of the failure mapping."""
    async with httpx2.AsyncClient(transport=_failing(httpx2.ConnectError("down"), "")) as http:
        with pytest.raises(ConnectionFailed, match="down"):
            await aload_manifest(http, BASE)
    async with httpx2.AsyncClient(transport=_failing(None, "<html>")) as http:
        with pytest.raises(ManifestError, match="not JSON"):
            await aload_manifest(http, BASE)
