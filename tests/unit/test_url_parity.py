"""URL parity with lib-crud-sdk-ts: every case of the shared table through the public client."""

import hashlib
import json
import re
from collections.abc import Callable
from datetime import date, datetime
from typing import Any, cast

import httpx2
import pytest

import craftainer_crud_sdk as sdk
from craftainer_crud_sdk import CrudClient, ResourceClient

from ._support import FIXTURES, make_transport

PARITY = FIXTURES.parent / "url-parity"
TABLE_FILE = PARITY / "url-parity.json"
TABLE: dict[str, Any] = json.loads(TABLE_FILE.read_text())
CASES: list[dict[str, Any]] = TABLE["cases"]
# The fixture is sent to https://api.test; the mock transport answers whatever is asked.
API = TABLE["baseUrl"]
# Calls typed to return a JSON array.
ARRAY_CALLS = {"list", "filters", "revisions"}


def _manifest_doc(name: str) -> dict[str, Any]:
    """A manifest named by the table: the shared fixtures, or the parity table's own."""
    for folder in (PARITY, FIXTURES):
        path = folder / name
        if path.exists():
            doc: dict[str, Any] = json.loads(path.read_text())
            return doc
    raise AssertionError(name)


def revive(value: object) -> object:
    """Turn the table's `$datetime` / `$date` markers into values (no offset means UTC)."""
    if isinstance(value, list):
        return [revive(item) for item in value]
    if isinstance(value, dict):
        if set(value) == {"$datetime"}:
            return datetime.fromisoformat(value["$datetime"].replace("Z", "+00:00"))
        if set(value) == {"$date"}:
            return date.fromisoformat(value["$date"])
        return {key: revive(item) for key, item in value.items()}
    return value


CALLS: dict[str, Callable[[ResourceClient, dict[str, Any]], object]] = {
    "list": lambda r, a: r.list(
        a.get("filters"),
        a.get("sort"),
        skip=a.get("skip"),
        limit=a.get("limit"),
        include_archived=a.get("includeArchived"),
        include_unpublished=a.get("includeUnpublished"),
    ),
    "get": lambda r, a: r.get(a["id"]),
    "create": lambda r, a: r.create(a["data"]),
    "update": lambda r, a: r.update(a["id"], a["data"], a.get("ifMatch")),
    "updateMany": lambda r, a: r.update_many(a["filters"], a["data"]),
    "delete": lambda r, a: r.delete(a["id"], a.get("ifMatch")),
    "deleteMany": lambda r, a: r.delete_many(a["filters"]),
    "restore": lambda r, a: r.restore(a.get("id"), a.get("filters")),
    "stats": lambda r, a: r.stats(
        a.get("bucket"), a.get("includeArchived"), a.get("includeUnpublished")
    ),
    "predict": lambda r, a: r.predict(a.get("field"), a.get("periods"), a.get("bucket")),
    "clone": lambda r, a: r.clone(a["id"]),
    "createDraft": lambda r, a: r.create_draft(a["data"]),
    "publish": lambda r, a: r.publish(a["id"]),
    "filters": lambda r, _a: r.filters(),
    "revisions": lambda r, a: r.revisions(a["id"]),
    "eventsUrl": lambda r, _a: r.events_url(),
}


def test_table_shape() -> None:
    """The table is the expected size and exercises every call the runner maps."""
    assert len(CASES) > 150
    assert {case["call"] for case in CASES} == set(CALLS)
    assert TABLE["baseUrl"] == API


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_url_parity(case: dict[str, Any]) -> None:
    """The Python client sends the method and URL the table pins (or raises the pinned error)."""
    seen: list[httpx2.Request] = []

    def other(request: httpx2.Request) -> httpx2.Response:
        """Answer any call with an empty body of the right JSON shape."""
        if case["call"] == "delete":
            return httpx2.Response(204)
        return httpx2.Response(200, json=[] if case["call"] in ARRAY_CALLS else {})

    transport = make_transport(other, seen=seen)
    # `make_transport` serves the fixture manifest; this table needs its own, per case.
    doc = _manifest_doc(case["manifest"])
    inner = transport

    def route(request: httpx2.Request) -> httpx2.Response:
        """Serve the case's manifest, everything else as `make_transport` does."""
        if request.url.path == "/api/_crud/manifest":
            seen.append(request)
            return httpx2.Response(200, json=doc, headers={"ETag": 'W/"m"'})
        return inner.handle_request(request)

    with CrudClient(API, transport=httpx2.MockTransport(route)) as client:
        resource = client.resource(case["resource"], case["apiVersion"])
        before = len(seen)
        args = cast("dict[str, Any]", revive(case["args"]))
        expect = case["expect"]
        if "error" in expect:
            with pytest.raises(getattr(sdk, expect["error"])):
                CALLS[case["call"]](resource, args)
            assert len(seen) == before
        elif case["call"] == "eventsUrl":
            assert CALLS["eventsUrl"](resource, args) == expect["url"]
        else:
            CALLS[case["call"]](resource, args)
            request = seen[-1]
            assert request.method == expect["method"]
            assert str(request.url) == expect["url"]


def test_fixture_matches_readme() -> None:
    """The vendored copy is the one the fixtures README records (sha256 per file)."""
    readme = (FIXTURES.parent / "README.md").read_text()
    for path in sorted(PARITY.glob("*.json")):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        match = re.search(rf"`{re.escape(path.name)}`[^\n]*?sha256 `([0-9a-f]{{64}})`", readme)
        assert match is not None, path.name
        assert match.group(1) == digest, f"{path.name} changed: refresh it (see the README)"
