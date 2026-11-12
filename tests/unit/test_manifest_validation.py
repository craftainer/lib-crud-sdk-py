"""OF1: manifest `name`, `base_path`, `paths` and `keepalive_seconds` fail closed."""

import copy
from typing import Any

import pytest

from craftainer_crud_sdk import ManifestError, parse_manifest
from craftainer_crud_sdk.keepalive import (
    DEFAULT_KEEPALIVE,
    MAX_KEEPALIVE,
    MIN_KEEPALIVE,
    clamp_keepalive,
)

from ._support import load_doc


def _doc(**changes: object) -> dict[str, Any]:
    doc = copy.deepcopy(load_doc())
    resource = doc["resources"][0]
    for key, value in changes.items():
        if key.startswith("paths."):
            resource["paths"][key.removeprefix("paths.")] = value
        elif key == "events_path":
            resource["capabilities"]["events"] = {
                **(resource["capabilities"]["events"] or {}),
                "path": value,
            }
        else:
            resource[key] = value
    return doc


def test_the_fixtures_are_valid() -> None:
    """The shipped manifests still parse."""
    assert parse_manifest(load_doc()).resources


@pytest.mark.parametrize(
    "name", ["", "a/b", "../x", "a b", "-lead", "a" * 65, "na\nme", "a:b", "é", "a.b"]
)
def test_a_bad_resource_name_is_refused(name: str) -> None:
    """A name that could alias another resource's cache prefix is not accepted."""
    with pytest.raises(ManifestError, match=r"\.name"):
        parse_manifest(_doc(name=name))


@pytest.mark.parametrize(
    "path",
    [
        "heroes",  # not absolute
        "//evil.example/x",  # protocol-relative
        "/a?x=1",
        "/a#frag",
        "/a\\b",
        "/a/../b",
        "/a/./b",
        "/a b",
        "/a\nb",
        "/a%2e%2e/b",
        "/a%2Fb",
        "/a%5cb",
        "/a%00",
        "/" + "a" * 1100,
        "ftp://x.example/a",
        "https://user:pw@evil.example/a",
        "https://crud.test/a?x=1",
        "https://crud.test/a#f",
        "https:///a",
        "https://crud.test/a/../b",
        "https://crud.test/" + "a" * 1100,
        "http://[bad/x",
        "https://exa\x00mple.test/a",
    ],
)
def test_a_bad_base_path_or_path_is_refused(path: str) -> None:
    """Every shape that would lose or merge the appended query, or climb out, is refused."""
    with pytest.raises(ManifestError, match="base_path"):
        parse_manifest(_doc(base_path=path))
    with pytest.raises(ManifestError, match=r"paths\.filters"):
        parse_manifest(_doc(**{"paths.filters": path}))


def test_an_event_stream_path_is_validated_too() -> None:
    """`capabilities.events.path` follows the same rule."""
    with pytest.raises(ManifestError, match=r"events\.path|\.path"):
        parse_manifest(_doc(events_path="/e?x=1"))
    parsed = parse_manifest(_doc(events_path="https://events.example.com/e"))
    assert parsed.resources[0].capabilities.events is not None


@pytest.mark.parametrize(
    "path", ["/", "/api/crud/v1/heroes/v2/json", "/a-b_c.d~e/f%20g/h:i@j", "https://crud.test/a"]
)
def test_valid_paths_pass(path: str) -> None:
    """RFC 3986 path characters and absolute http(s) URLs (checked at join time) are accepted."""
    assert parse_manifest(_doc(base_path=path)).resources[0].base_path == path


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (15, 15.0),
        (0.2, MIN_KEEPALIVE),
        (1e9, MAX_KEEPALIVE),
        (float("inf"), DEFAULT_KEEPALIVE),
        (float("nan"), DEFAULT_KEEPALIVE),
        (0, DEFAULT_KEEPALIVE),
        (-5, DEFAULT_KEEPALIVE),
        (True, DEFAULT_KEEPALIVE),
        ("10", DEFAULT_KEEPALIVE),
        (None, DEFAULT_KEEPALIVE),
        (10**400, DEFAULT_KEEPALIVE),
    ],
)
def test_keepalive_is_clamped(raw: object, expected: float) -> None:
    """A huge or odd keep-alive cannot switch dead-stream detection off."""
    assert clamp_keepalive(raw) == expected


def test_the_parsed_manifest_carries_the_clamped_keepalive() -> None:
    """`Events.keepalive_seconds` is the value actually used, and a huge int does not crash."""
    for raw, expected in ((1e9, MAX_KEEPALIVE), (10**400, DEFAULT_KEEPALIVE), (3, 3.0)):
        doc = _doc()
        doc["resources"][0]["capabilities"]["events"]["keepalive_seconds"] = raw
        events = parse_manifest(doc).resources[0].capabilities.events
        assert events is not None
        assert events.keepalive_seconds == expected
