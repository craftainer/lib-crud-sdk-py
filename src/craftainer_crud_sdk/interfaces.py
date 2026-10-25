"""Contracts shared by the client core (PY1) and the cache layer (PY2). Fixed in PY0 (I2)."""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol, TypedDict, runtime_checkable

import httpx2

__all__ = [
    "EXTENSION_KEY",
    "AsyncCacheStore",
    "AsyncTokenProvider",
    "AsyncTransportHook",
    "BindsTransport",
    "CacheEntry",
    "CacheStore",
    "CrudEvent",
    "Op",
    "RequestContext",
    "SendRaw",
    "SyncSendRaw",
    "TokenProvider",
    "TransportHook",
]

type Op = Literal[
    "manifest", "list", "record", "filters", "stats", "predict", "revisions",
    "create", "update", "delete", "bulk_update", "bulk_delete", "clone",
    "draft", "publish", "restore", "events",
]  # fmt: skip

EXTENSION_KEY = "craftainer_crud"
"""`httpx2.Request.extensions[EXTENSION_KEY]` holds the request's `RequestContext`."""


class RequestContext(TypedDict):
    """What the client knows about a request that the bare `httpx2.Request` does not.

    The client sets it on every request it builds; hooks read it, so the cache layer never
    parses URLs to learn the resource or record id, and never reads the Authorization header.
    """

    resource: str | None  # manifest name; None for the manifest document itself
    op: Op
    record_id: int | None
    api_version: str | None  # resource api_version (e.g. "v2"); None for manifest/catalog requests
    scope: str  # "anon", or a hex digest of the caller identity (auth.cache_scope)


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """One stored `200` response. Times are epoch seconds (`time.time()`)."""

    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes
    stored_at: float
    fresh_until: float  # <= stored_at means "always revalidate"
    stale_until: float  # end of the stale-while-revalidate window (>= fresh_until)
    etag: str | None = None


class CacheStore(Protocol):
    """Synchronous cache backend. Keys are opaque strings built by `cache.keys` (slash-separated).

    `ttl` is how long the backend may keep the entry (seconds, `None` = backend default); it is
    not freshness, which lives in `CacheEntry`. `clear(prefix)` deletes every key that starts
    with `prefix`; `clear()` deletes everything this store owns (never other data in a shared
    Redis). Implementations must be thread-safe and must not raise on a missing key. A `clear`
    (and an optional `clear_many(prefixes)`) that fails without raising returns `False`: the
    layer then treats the affected resources as not fresh until a later reset succeeds.
    """

    def get(self, key: str) -> CacheEntry | None:
        """Return the entry, or `None` if absent or expired."""
        ...

    def set(self, key: str, entry: CacheEntry, ttl: float | None = None) -> None:
        """Store `entry` under `key`, replacing any previous value."""
        ...

    def delete(self, key: str) -> None:
        """Remove `key` if present."""
        ...

    def clear(self, prefix: str | None = None) -> bool | None:
        """Remove every key starting with `prefix`, or all keys when `prefix` is `None`.

        Returns `False` if the keys could not be removed (the store logged why).
        """
        ...


class AsyncCacheStore(Protocol):
    """Asynchronous twin of `CacheStore`, for `httpx2.AsyncClient` and `redis.asyncio`."""

    async def get(self, key: str) -> CacheEntry | None:
        """Return the entry, or `None` if absent or expired."""
        ...

    async def set(self, key: str, entry: CacheEntry, ttl: float | None = None) -> None:
        """Store `entry` under `key`, replacing any previous value."""
        ...

    async def delete(self, key: str) -> None:
        """Remove `key` if present."""
        ...

    async def clear(self, prefix: str | None = None) -> bool | None:
        """Remove every key starting with `prefix`, or all keys when `prefix` is `None`.

        Returns `False` if the keys could not be removed (the store logged why).
        """
        ...


type SyncSendRaw = Callable[[httpx2.Request], httpx2.Response]
type SendRaw = Callable[[httpx2.Request], Awaitable[httpx2.Response]]


class TransportHook(Protocol):
    """Synchronous transport hook, run by the client's wrapper around `httpx2.BaseTransport`.

    Order: `on_request` of hooks[0..n] in registration order; the first one that returns a
    response short-circuits the network call (a fresh cache hit). `on_response` then runs in
    reverse order, for the hooks that ran `on_request` *before* the short-circuiting one and for
    real network responses; it may return a different response (a `304` rebuilt from the cache).
    A hook must not read the response stream unless it replaces the response.
    """

    def on_request(self, request: httpx2.Request) -> httpx2.Response | None:
        """Mutate `request` (e.g. add `If-None-Match`) or return a response to skip the network."""
        ...

    def on_response(self, request: httpx2.Request, response: httpx2.Response) -> httpx2.Response:
        """Observe or replace the response (after any hooks' `on_request` ran)."""
        ...


class AsyncTransportHook(Protocol):
    """Asynchronous twin of `TransportHook`, run around `httpx2.AsyncBaseTransport`."""

    async def on_request(self, request: httpx2.Request) -> httpx2.Response | None:
        """Mutate `request` or return a response to skip the network."""
        ...

    async def on_response(
        self, request: httpx2.Request, response: httpx2.Response
    ) -> httpx2.Response:
        """Observe or replace the response."""
        ...


@runtime_checkable
class BindsTransport(Protocol):
    """Optional: hooks that need to send requests themselves (background revalidation).

    The client calls `bind` once with a function that sends below all hooks (they do not run for
    it, so the hook applies its own `on_response` to what comes back) but through the client's
    authentication: the token provider's token is set (replacing an ``Authorization`` header on the
    request passed in; without a token it stays) and a ``401`` is refreshed and retried once, like
    any request.
    """

    def bind(self, send: SyncSendRaw | SendRaw) -> None:
        """Receive the authenticated, hook-free send function."""
        ...


class TokenProvider(Protocol):
    """Returns the current bearer token, or `None` for an anonymous call. Synchronous."""

    def __call__(self, *, refresh: bool = False) -> str | None:
        """`refresh=True` is passed once after a `401`; return a fresh token if you can."""
        ...


class AsyncTokenProvider(Protocol):
    """Asynchronous twin of `TokenProvider`."""

    async def __call__(self, *, refresh: bool = False) -> str | None:
        """`refresh=True` is passed once after a `401`; return a fresh token if you can."""
        ...


@dataclass(frozen=True, slots=True)
class CrudEvent:
    """One record-mutation event from `GET {base_path}/events` (server `_event_envelope`)."""

    resource: str  # the server's event label (e.g. "hero"), not the manifest name
    record_id: int
    action: Literal["create", "update", "delete", "restore"]
    snapshot: Mapping[str, object]  # the read view of the record after the change
    timestamp: str  # ISO 8601, UTC
