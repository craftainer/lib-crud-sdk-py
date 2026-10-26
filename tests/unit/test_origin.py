"""Request URLs built from the manifest stay on `base_url`'s origin unless opted in."""

import copy
from typing import Any

import httpx2
import pytest

from craftainer_crud_sdk import (
    AsyncCrudClient,
    AsyncMemoryStore,
    CrudClient,
    ManifestError,
    MemoryStore,
    ResourceClient,
    aenable_cache,
    enable_cache,
)
from craftainer_crud_sdk.origin import join_same_origin

from ._support import BASE, CATALOG, MANIFEST_PATH, PROFILE, load_doc

EVIL = "https://evil.example"
CATALOG_PATH = "/.well-known/api-catalog"


@pytest.mark.parametrize(
    "ref",
    [
        f"{EVIL}/x",
        "//evil.example/x",
        "http://crud.test/x",  # same host, other scheme
        "https://crud.test:8443/x",  # same host, other port
        "https://crud.test.evil.example/x",
        "https://crud.test@evil.example/x",
    ],
)
def test_foreign_urls_are_rejected(ref: str) -> None:
    """Absolute and protocol-relative URLs on another origin raise ManifestError."""
    with pytest.raises(ManifestError, match="allowed_origins"):
        join_same_origin(BASE, ref)


@pytest.mark.parametrize(
    "ref", ["/a/b", "a/b", "", "?q=1", f"{BASE}/x", "//crud.test/x", "https://CRUD.test:443/x"]
)
def test_same_origin_urls_are_joined(ref: str) -> None:
    """Relative references and same-origin absolute ones pass, default port included."""
    assert httpx2.URL(join_same_origin(BASE, ref)).host == "crud.test"


def test_allowed_origins_opt_in() -> None:
    """A listed origin is accepted (path ignored, default port normalised); others are not."""
    assert join_same_origin(BASE, f"{EVIL}/x", [EVIL]) == f"{EVIL}/x"
    assert join_same_origin(BASE, "//evil.example/x", ["https://EVIL.example:443/ignored"])
    with pytest.raises(ManifestError):
        join_same_origin(BASE, f"{EVIL}/x", ["https://other.example"])


def test_a_non_http_scheme_is_rejected_and_bad_allowed_origins_fail_early() -> None:
    """Only http(s) origins are comparable; a bad `allowed_origins` entry is a ValueError."""
    with pytest.raises(ManifestError):
        join_same_origin(BASE, "ftp://crud.test/x")
    for bad in ("events.example.com", "ftp://x.example", "http://"):
        with pytest.raises(ValueError, match="allowed_origins"):
            CrudClient(BASE, allowed_origins=[bad])


def test_a_catalog_or_manifest_redirect_is_a_manifest_error() -> None:
    """The discovery requests are never followed either."""
    transport = httpx2.MockTransport(
        lambda r: httpx2.Response(302, headers={"Location": "https://evil.example/c"})
    )
    with (
        CrudClient(BASE, transport=transport) as client,
        pytest.raises(ManifestError, match="never follows redirects"),
    ):
        client.manifest()


def test_an_unparseable_url_is_a_manifest_error() -> None:
    """A URL httpx cannot parse raises ManifestError, not a bare httpx error."""
    with pytest.raises(ManifestError, match="not a valid URL"):
        join_same_origin(BASE, "http://[bad/x")


def _doc(base_path: str | None = None, events_path: str | None = None) -> dict[str, Any]:
    doc = copy.deepcopy(load_doc())
    for resource in doc["resources"]:
        if resource["name"] == "heroes" and resource["api_version"] == "v2":
            if base_path is not None:
                resource["base_path"] = base_path
            if events_path is not None:
                resource["capabilities"]["events"]["path"] = events_path
    return doc


def _transport(
    doc: dict[str, Any],
    seen: list[httpx2.Request],
    catalog: dict[str, Any] | None = None,
) -> httpx2.MockTransport:
    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if request.url.path == CATALOG_PATH:
            return httpx2.Response(200, json=catalog or CATALOG)
        if request.url.path.endswith("manifest") or request.url.path == MANIFEST_PATH:
            return httpx2.Response(200, json=doc)
        return httpx2.Response(200, json=[])

    return httpx2.MockTransport(handler)


def _heroes(client: CrudClient) -> ResourceClient:
    return client.resource("heroes", "v2")


@pytest.mark.parametrize("base_path", [f"{EVIL}/heroes", "//evil.example/heroes"])
def test_a_foreign_base_path_never_sends_the_token(base_path: str) -> None:
    """The request is refused before anything is sent."""
    seen: list[httpx2.Request] = []
    with (
        CrudClient(
            BASE,
            transport=_transport(_doc(base_path=base_path), seen),
            token_provider=lambda *, refresh=False: "tok",
        ) as client,
        pytest.raises(ManifestError, match="not on the origin"),
    ):
        _heroes(client).list()
    assert all(r.url.host == "crud.test" for r in seen)
    assert len(seen) == 2  # catalog and manifest only


def test_allowed_origins_permit_a_split_origin_deployment() -> None:
    """`allowed_origins` lets the manifest point a path at another listed origin."""
    seen: list[httpx2.Request] = []
    transport = _transport(_doc(base_path=f"{EVIL}/heroes"), seen)
    with CrudClient(BASE, transport=transport, allowed_origins=[EVIL]) as client:
        assert _heroes(client).list() == []
        assert client.allowed_origins == (EVIL,)
    assert seen[-1].url.host == "evil.example"


def test_a_foreign_events_url_is_rejected_by_events_url_and_enable_cache() -> None:
    """The SSE URL is joined at two sites; both refuse a foreign host."""
    doc = _doc(events_path="//evil.example/events")
    with CrudClient(BASE, transport=_transport(doc, [])) as client:
        with pytest.raises(ManifestError):
            _heroes(client).events_url()
        with pytest.raises(ManifestError):
            enable_cache(client, MemoryStore(), resources=["heroes"])
    with CrudClient(
        BASE, transport=_transport(doc, []), allowed_origins=["https://evil.example"]
    ) as ok:
        assert _heroes(ok).events_url() == "https://evil.example/events"


def test_a_foreign_describedby_href_is_rejected_but_manifest_url_is_trusted() -> None:
    """Discovery refuses a foreign manifest link; an explicit manifest_url is the caller's."""
    catalog = {"linkset": [{"describedby": [{"href": f"{EVIL}/manifest", "profile": PROFILE}]}]}
    seen: list[httpx2.Request] = []
    with (
        CrudClient(BASE, transport=_transport(_doc(), seen, catalog)) as client,
        pytest.raises(ManifestError, match="not on the origin"),
    ):
        client.manifest()
    assert [r.url.host for r in seen] == ["crud.test"]
    seen.clear()
    with CrudClient(
        BASE, transport=_transport(_doc(), seen, catalog), manifest_url=f"{EVIL}/manifest"
    ) as trusted:
        assert trusted.manifest().resources
    assert [r.url.host for r in seen] == ["evil.example"]
    seen.clear()
    with CrudClient(
        BASE, transport=_transport(_doc(), seen, catalog), allowed_origins=[EVIL]
    ) as allowed:
        assert allowed.manifest().resources


@pytest.mark.asyncio
async def test_async_client_applies_the_same_rules() -> None:
    """Async: paths, events, discovery and allowed_origins."""
    seen: list[httpx2.Request] = []
    async with AsyncCrudClient(
        BASE, transport=_transport(_doc(base_path="//evil.example/h"), seen)
    ) as client:
        heroes = await client.resource("heroes", "v2")
        with pytest.raises(ManifestError):
            await heroes.list()
    assert all(r.url.host == "crud.test" for r in seen)
    async with AsyncCrudClient(
        BASE, transport=_transport(_doc(events_path=f"{EVIL}/e"), [])
    ) as client:
        heroes = await client.resource("heroes", "v2")
        with pytest.raises(ManifestError):
            heroes.events_url()
        with pytest.raises(ManifestError):
            await aenable_cache(client, AsyncMemoryStore(MemoryStore()), resources=["heroes"])
    catalog = {"linkset": [{"describedby": [{"href": "//evil.example/m", "profile": PROFILE}]}]}
    async with AsyncCrudClient(BASE, transport=_transport(_doc(), [], catalog)) as client:
        with pytest.raises(ManifestError):
            await client.manifest()
    async with AsyncCrudClient(
        BASE, transport=_transport(_doc(base_path=f"{EVIL}/h"), seen), allowed_origins=[EVIL]
    ) as client:
        heroes = await client.resource("heroes", "v2")
        assert await heroes.list() == []
