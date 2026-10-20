"""Tests for the I2 contracts."""

import dataclasses

import httpx2
import pytest

import craftainer_crud_sdk
from craftainer_crud_sdk import BindsTransport, CacheEntry, CrudEvent, TransportHook


class _Binder:
    def __init__(self) -> None:
        self.sent: object = None

    def bind(self, send: craftainer_crud_sdk.SyncSendRaw | craftainer_crud_sdk.SendRaw) -> None:
        self.sent = send


class _Hook:
    def on_request(self, request: httpx2.Request) -> httpx2.Response | None:
        return None

    def on_response(self, request: httpx2.Request, response: httpx2.Response) -> httpx2.Response:
        return response


def _entry() -> CacheEntry:
    return CacheEntry(200, (("etag", '"a"'),), b"{}", 1.0, 2.0, 3.0, '"a"')


def test_binds_transport_is_runtime_checkable() -> None:
    """A class with bind() is a BindsTransport; one without is not."""
    binder = _Binder()
    assert isinstance(binder, BindsTransport)
    assert not isinstance(_Hook(), BindsTransport)
    binder.bind(lambda request: httpx2.Response(200, request=request))
    assert binder.sent is not None


def test_minimal_hook_conforms() -> None:
    """A minimal conforming hook type-checks as a TransportHook and passes responses through."""
    hook: TransportHook = _Hook()
    request = httpx2.Request("GET", "http://x/")
    response = httpx2.Response(200, request=request)
    assert hook.on_request(request) is None
    assert hook.on_response(request, response) is response


def test_cache_entry_is_frozen() -> None:
    """CacheEntry cannot be mutated and defaults etag to None."""
    entry = _entry()
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.status = 404  # type: ignore[misc]
    assert CacheEntry(200, (), b"", 0.0, 0.0, 0.0).etag is None


def test_crud_event_is_frozen() -> None:
    """CrudEvent is an immutable value."""
    event = CrudEvent("hero", 1, "create", {"id": 1}, "2026-01-01T00:00:00Z")
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.record_id = 2  # type: ignore[misc]


def test_all_exports_resolve() -> None:
    """Every name in __all__ exists on the package."""
    assert craftainer_crud_sdk.EXTENSION_KEY == "craftainer_crud"
    for name in craftainer_crud_sdk.__all__:
        assert hasattr(craftainer_crud_sdk, name)
