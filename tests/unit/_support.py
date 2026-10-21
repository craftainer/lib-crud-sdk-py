"""Shared test helpers: fixtures and a routing mock transport."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2

from craftainer_crud_sdk import Manifest, Resource, parse_manifest

FIXTURES = Path(__file__).parent.parent / "fixtures" / "manifest"
PROFILE = "https://craftainer.github.io/schemas/crud-manifest/v1"
MANIFEST_PATH = "/api/_crud/manifest"
BASE = "http://crud.test"
HEROES_V2 = "/api/crud/v1/heroes/v2/json"
HEROES_V1 = "/api/crud/v1/heroes/v1/json"

CASES = ["admin", "anonymous", "hero-reader", "legacy-reader", "stranger"]


def load_doc(case: str = "admin") -> dict[str, Any]:
    """The raw fixture document."""
    doc: dict[str, Any] = json.loads((FIXTURES / f"{case}.manifest.json").read_text())
    return doc


def load(case: str = "admin") -> Manifest:
    """A parsed fixture."""
    return parse_manifest(load_doc(case))


def resource(name: str = "heroes", version: str = "v2") -> Resource:
    """A resource of the admin fixture."""
    found = load().find(name, version)
    assert found is not None
    return found


CATALOG = {
    "linkset": [
        {
            "anchor": f"{BASE}/api",
            "service-desc": [{"href": "/openapi.json"}],
            "describedby": [{"href": MANIFEST_PATH, "profile": PROFILE}],
        }
    ]
}


def make_transport(
    other: Callable[[httpx2.Request], httpx2.Response],
    case: str = "admin",
    seen: list[httpx2.Request] | None = None,
) -> httpx2.MockTransport:
    """Serve the catalog and a fixture manifest; everything else goes to `other`."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        """Handler."""
        if seen is not None:
            seen.append(request)
        if request.url.path == "/.well-known/api-catalog":
            return httpx2.Response(200, json=CATALOG)
        if request.url.path == MANIFEST_PATH:
            return httpx2.Response(200, json=load_doc(case), headers={"ETag": 'W/"m"'})
        return other(request)

    return httpx2.MockTransport(handler)
