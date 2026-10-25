"""The caching transport hooks: `CacheLayer` (sync) and `AsyncCacheLayer` (async, with SWR).

Both hooks run the same decisions (`_Core`); only the store calls and the body read differ.
They read the resource, operation, record id and caller scope from the request's
`RequestContext` extension and never touch the Authorization header.

A read is answered in one of four ways: ``hit`` (fresh, no network), ``stale`` (async only: stale
but inside the stale-while-revalidate window, refreshed in the background), ``revalidated`` (a
``304`` rebuilt from the stored entry with merged headers) or ``miss``. How the cache answered
is in ``response.extensions["craftainer_cache"]``.

Freshness beyond "revalidate every time" is granted only while the resource's SSE stream is up
(`set_events_active`): events are the only thing that can end a fresh window early. ``stats`` and
``predict`` are bounded by ``stats_ttl`` instead, as the server caches those bodies the same way.
"""

import asyncio
import json
import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from http import HTTPStatus
from typing import cast

import httpx2

from craftainer_crud_sdk.cache.entry import (
    CACHE_STATUS_EXTENSION,
    build_entry,
    cache_control,
    is_json,
    merge_headers,
    to_response,
    vary_allows_caching,
)
from craftainer_crud_sdk.cache.keys import (
    KINDS,
    STATS_KINDS,
    UNVERSIONED,
    build_key,
    event_prefixes,
    kind_prefix,
    manifest_key,
    record_prefix,
    resource_prefixes,
    revisions_prefix,
)
from craftainer_crud_sdk.cache.policy import CachePolicy
from craftainer_crud_sdk.interfaces import (
    EXTENSION_KEY,
    AsyncCacheStore,
    CacheEntry,
    CacheStore,
    RequestContext,
    SendRaw,
    SyncSendRaw,
)

__all__ = ["AsyncCacheLayer", "CacheLayer"]

log = logging.getLogger(__name__)

STATE_KEY = "craftainer_cache_state"
_MIN_RETENTION = 3600.0
_MANIFEST_POLICY = CachePolicy()
_COLLECTIONS = ("list", "stats", "predict")
_BULK_OPS = frozenset({"bulk_update", "bulk_delete"})
_RECORD_WRITES = frozenset({"delete", "publish", "restore"})
_CREATES = frozenset({"create", "clone", "draft"})
_RESOURCE_PARTS = 2  # "v1", resource
_KIND_PARTS = 3  # "v1", resource, kind
_WRITE_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})

type PolicyKey = str | tuple[str, str]
"""A resource name (all its versions) or ``(name, api_version)`` (``_`` when unversioned)."""

type _Decision = str  # "hit" | "stale" | "revalidate" | "miss"


@dataclass(frozen=True, slots=True)
class _Clear:
    prefix: str


@dataclass(frozen=True, slots=True)
class _Set:
    key: str
    entry: CacheEntry
    ttl: float


@dataclass(frozen=True, slots=True)
class _Delete:
    key: str


type _Op = _Clear | _Set | _Delete


@dataclass(slots=True)
class _State:
    """What `on_request` learned, handed to `on_response` through the request extensions."""

    ctx: RequestContext
    policy: CachePolicy
    kind: str | None  # cache kind of a read; None for a write
    key: str | None
    api_version: str  # `RequestContext.api_version`, `_` when None
    generation: int  # the resource's invalidation count when the request was made
    priced: int | None = None  # the generation `plan` priced the new entry against
    entry: CacheEntry | None = None
    force_revalidate: bool = False
    validated: bool = False


class _Core:
    """Shared decisions of the sync and async layers; performs no I/O."""

    def __init__(
        self,
        policies: Mapping[PolicyKey, CachePolicy],
        *,
        cache_manifest: bool,
        clock: Callable[[], float],
    ) -> None:
        self.policies: dict[str, CachePolicy] = {}
        self.versioned: dict[tuple[str, str], CachePolicy] = {}
        for key, policy in policies.items():
            if isinstance(key, str):
                self.policies[key] = policy
            else:
                self.versioned[key] = policy
        self.cache_manifest = cache_manifest
        self.clock = clock
        self._events_active: dict[str, int] = {}  # connected streams per resource (the gate)
        self._unsafe: set[str] = set()  # resources whose last clear failed: never fresh
        self._unsafe_all = False  # a failed clear of unknown scope: no resource is fresh
        self._lock = threading.Lock()
        self._generations: dict[str, int] = {}

    # -- invalidation generations ----------------------------------------------------------

    def generation(self, resource: str | None) -> int:
        """How many invalidations of ``resource`` have landed so far."""
        return self._generations.get(resource or "", 0)

    def bump(self, resource: str | None) -> int:
        """Record an invalidation of ``resource``; returns the new generation."""
        with self._lock:
            generation = self._generations.get(resource or "", 0) + 1
            self._generations[resource or ""] = generation
            return generation

    def finish(self, state: _State, ops: list[_Op]) -> int:
        """Before applying ``ops``: bump if a write clears; return the generation expected after.

        A read expects the generation `plan` priced its entry against, not one read now: an
        invalidation that landed in between must be seen by `late_deletes`.
        """
        if state.kind is None and any(isinstance(op, _Clear) for op in ops):
            return self.bump(state.ctx["resource"])
        return state.generation if state.priced is None else state.priced

    def late_deletes(self, state: _State, ops: list[_Op], expected: int) -> list[_Delete]:
        """Deletes for the entries just stored if an invalidation landed while storing them."""
        if self.generation(state.ctx["resource"]) == expected:
            return []
        return [_Delete(op.key) for op in ops if isinstance(op, _Set)]

    # -- control surface (shared by both layers) -------------------------------------------

    def event_clears(self, resource: str, action: str, record_id: int | None) -> list[str]:
        """Record an SSE event's invalidation; the prefixes it clears, in every version."""
        self.bump(resource)
        return event_prefixes(resource, action, record_id)

    def reset_clears(self, resource: str) -> list[str]:
        """Record a resource reset (after a reconnect); the prefixes it clears."""
        self.bump(resource)
        return resource_prefixes(resource)

    @staticmethod
    def etag_key(resource: str, record_id: int, scope: str, api_version: str | None) -> str:
        """Key of a record entry of one API version (``_`` if None), for ``auto_if_match``."""
        return build_key(
            resource, "record", record_id, scope, api_version=api_version or UNVERSIONED
        )

    # -- SSE gate ---------------------------------------------------------------------------

    def set_events_active(self, resource: str, active: bool) -> None:
        """A stream of ``resource`` connected (``True``) or went away (``False``): ref-counted."""
        with self._lock:
            count = self._events_active.get(resource, 0) + (1 if active else -1)
            if count > 0:
                self._events_active[resource] = count
            else:
                self._events_active.pop(resource, None)

    def events_active(self, resource: str) -> bool:
        return resource in self._events_active

    def clear_failed(self, prefixes: Sequence[str | None]) -> None:
        """A store could not clear ``prefixes``: the resources they name are not fresh until reset.

        A prefix that names no resource (or ``None``: everything) affects every resource.
        """
        with self._lock:
            for prefix in prefixes:
                parts = (prefix or "").split("/")
                if len(parts) < _RESOURCE_PARTS or parts[0] != "v1" or not parts[1]:
                    self._unsafe_all = True
                elif len(parts) < _KIND_PARTS or parts[2] != "filters":  # filters are never fresh
                    self._unsafe.add(parts[1])

    def cleared(self, resource: str | None) -> None:
        """A store cleared everything cached for ``resource`` (``None``: for every resource)."""
        with self._lock:
            if resource is None:
                self._unsafe.clear()
                self._unsafe_all = False
            else:
                self._unsafe.discard(resource)

    def _unsafe_resource(self, resource: str | None) -> bool:
        return self._unsafe_all or (resource is not None and resource in self._unsafe)

    def policy_for(self, resource: str | None, version: str) -> CachePolicy | None:
        """The policy of one (resource, api_version), else the resource's, else ``None``."""
        if resource is None:
            return None
        return self.versioned.get((resource, version)) or self.policies.get(resource)

    # -- request side -----------------------------------------------------------------------

    def begin(self, request: httpx2.Request) -> _State | None:
        ctx: RequestContext | None = request.extensions.get(EXTENSION_KEY)
        if ctx is None:
            return None
        resource = ctx["resource"]
        version = ctx["api_version"] or UNVERSIONED
        generation = self.generation(resource)
        if request.method != "GET":
            return self._begin_write(request, ctx, version, generation)
        target = self._read_target(request, ctx, version)
        if target is None:
            return None
        policy, kind, key = target
        control = cache_control(request.headers)
        prior: _State | None = request.extensions.get(STATE_KEY)
        if prior is not None and prior.validated:
            request.headers.pop("if-none-match", None)  # our own validator, on a retry
        if "no-store" in control or "if-none-match" in request.headers:
            return None  # the caller manages validators or forbids storing
        return _State(
            ctx, policy, kind, key, version, generation, force_revalidate="no-cache" in control
        )

    def _begin_write(
        self, request: httpx2.Request, ctx: RequestContext, version: str, generation: int
    ) -> _State | None:
        """State of a write; HEAD, OPTIONS and the like neither read nor change a stored entry."""
        if request.method not in _WRITE_METHODS:
            return None
        policy = self.policy_for(ctx["resource"], version)
        if policy is None or not policy.enabled:
            return None
        return _State(ctx, policy, None, None, version, generation)

    def _read_target(
        self, request: httpx2.Request, ctx: RequestContext, version: str
    ) -> tuple[CachePolicy, str, str] | None:
        """Policy, cache kind and key of a cacheable read, or ``None``."""
        resource = ctx["resource"]
        op = ctx["op"]
        if op == "manifest":
            if not self.cache_manifest:
                return None
            return _MANIFEST_POLICY, "manifest", manifest_key(ctx["scope"])
        found = self.policy_for(resource, version)
        if found is None or not found.enabled or op not in KINDS or resource is None:
            return None
        key = build_key(
            resource, op, ctx["record_id"], ctx["scope"], request.url.query, api_version=version
        )
        return found, op, key

    def _fresh_allowed(self, policy: CachePolicy, resource: str | None, kind: str) -> bool:
        """Whether an entry of ``kind`` may be served fresh (without a request) right now.

        ``stats``/``predict`` are bounded by ``stats_ttl`` and need no stream; every other kind
        needs the resource's stream. ``manifest`` and ``filters`` are never fresh.
        """
        if policy.revalidate_only or kind in {"manifest", "filters"}:
            return False
        if self._unsafe_resource(resource):
            return False
        return kind in STATS_KINDS or self._stream_up(resource)

    def _swr_allowed(self, policy: CachePolicy, resource: str | None, kind: str) -> bool:
        """Whether a stale entry may be served while it refreshes: only while the stream is up."""
        if policy.revalidate_only or kind in {"manifest", "filters"}:
            return False
        return self._stream_up(resource)

    def _stream_up(self, resource: str | None) -> bool:
        if self._unsafe_resource(resource):
            return False
        return resource is not None and resource in self._events_active

    def _times(self, policy: CachePolicy, resource: str | None, kind: str) -> tuple[float, float]:
        """Seconds fresh and seconds of stale-while-revalidate granted to a new entry."""
        fresh = 0.0
        if self._fresh_allowed(policy, resource, kind):
            fresh = policy.stats_ttl if kind in STATS_KINDS else policy.max_age
        stale = policy.stale_while_revalidate if self._swr_allowed(policy, resource, kind) else 0.0
        return fresh, stale

    def _entry_times(self, state: _State, kind: str) -> tuple[float, float]:
        """`_times` for a response; none if an invalidation landed while it was in flight."""
        resource = state.ctx["resource"]
        state.priced = generation = self.generation(resource)  # read once: the price and the check
        if generation != state.generation:
            return 0.0, 0.0  # at most revalidate-only: the body may predate the invalidation
        return self._times(state.policy, resource, kind)

    @staticmethod
    def _retention(fresh: float, stale: float) -> float:
        return max(fresh + stale, _MIN_RETENTION)

    def decide(self, state: _State, entry: CacheEntry | None, *, swr: bool) -> _Decision:
        if entry is None:
            return "miss"
        resource = state.ctx["resource"]
        kind = cast("str", state.kind)
        if not state.force_revalidate:
            now = self.clock()
            if self._fresh_allowed(state.policy, resource, kind) and now < entry.fresh_until:
                return "hit"
            if swr and self._swr_allowed(state.policy, resource, kind) and now < entry.stale_until:
                return "stale"
        return "revalidate" if entry.etag is not None and state.policy.etag else "miss"

    @staticmethod
    def add_validator(request: httpx2.Request, state: _State) -> None:
        etag = cast("CacheEntry", state.entry).etag
        if etag is not None:  # a stale entry without a validator is refetched plainly
            request.headers["If-None-Match"] = etag
            state.validated = True

    # -- response side ----------------------------------------------------------------------

    def wants_body(self, state: _State, response: httpx2.Response) -> bool:
        status = response.status_code
        if state.kind is not None:
            return status == HTTPStatus.OK and self._storable(response)
        ctx = state.ctx
        if status == HTTPStatus.OK and ctx["op"] == "update":
            return ctx["record_id"] is not None
        return HTTPStatus.OK <= status < HTTPStatus.MULTIPLE_CHOICES and (
            ctx["op"] in _BULK_OPS or (ctx["op"] == "restore" and ctx["record_id"] is None)
        )

    @staticmethod
    def _storable(response: httpx2.Response) -> bool:
        return (
            is_json(response)
            and "no-store" not in cache_control(response.headers)
            and vary_allows_caching(response)
        )

    def plan(
        self, state: _State, request: httpx2.Request, response: httpx2.Response
    ) -> tuple[httpx2.Response, list[_Op]]:
        if state.kind is None:
            return response, self._write_ops(state, response)
        return self._read_plan(state, request, response)

    def _read_plan(
        self, state: _State, request: httpx2.Request, response: httpx2.Response
    ) -> tuple[httpx2.Response, list[_Op]]:
        key = cast("str", state.key)
        kind = cast("str", state.kind)
        status = response.status_code
        now = self.clock()
        fresh, stale = self._entry_times(state, kind)
        entry = state.entry
        if status == HTTPStatus.NOT_MODIFIED and state.validated and entry is not None:
            etag = response.headers.get("etag", entry.etag) if state.policy.etag else None
            updated = replace(
                entry,
                headers=merge_headers(entry.headers, response.headers),
                stored_at=now,
                fresh_until=now + fresh,
                stale_until=now + fresh + stale,
                etag=etag,
            )
            rebuilt = to_response(
                updated,
                request,
                status="revalidated",
                extensions=response.extensions,
                set_cookies=response.headers.get_list("set-cookie"),
            )
            return rebuilt, [_Set(key, updated, self._retention(fresh, stale))]
        if status == HTTPStatus.OK and self._storable(response):
            response.extensions[CACHE_STATUS_EXTENSION] = "miss"
            new = build_entry(
                response, now=now, fresh=fresh, stale=stale, keep_etag=state.policy.etag
            )
            if new.etag is None and fresh <= 0:
                return response, [_Delete(key)]  # no validator and never fresh: drop the old one
            ops: list[_Op] = [_Set(key, new, self._retention(fresh, stale))]
            if kind == "manifest":
                ops.extend(self._manifest_ops(entry, new))
            return response, ops
        if status in {HTTPStatus.OK, HTTPStatus.NOT_FOUND, HTTPStatus.GONE}:
            return response, [_Delete(key)]
        return response, []

    def _manifest_ops(self, old: CacheEntry | None, new: CacheEntry) -> list[_Op]:
        """A changed manifest ETag invalidates every resource's cached ``filters``."""
        if old is None or old.etag == new.etag:
            return []
        names = (
            set(self.policies)
            | {n for n, _ in self.versioned}
            | _manifest_resources(old.body)
            | _manifest_resources(new.body)
        )
        return [_Clear(kind_prefix(name, "filters")) for name in sorted(names)]

    def _write_ops(self, state: _State, response: httpx2.Response) -> list[_Op]:
        ctx = state.ctx
        resource = cast("str", ctx["resource"])
        record_id = ctx["record_id"]
        op = ctx["op"]
        status = response.status_code
        if status == HTTPStatus.PRECONDITION_FAILED:
            return [] if record_id is None else [_Clear(record_prefix(resource, record_id))]
        if not HTTPStatus.OK <= status < HTTPStatus.MULTIPLE_CHOICES:
            return []
        collections: list[_Op] = [_Clear(kind_prefix(resource, kind)) for kind in _COLLECTIONS]
        if op in _CREATES:
            return collections
        if record_id is not None and (op in _RECORD_WRITES or op == "update"):
            ops: list[_Op] = [
                *collections,
                _Clear(revisions_prefix(resource, record_id)),
                _Clear(record_prefix(resource, record_id)),
            ]
            if op == "update":
                ops.extend(self._write_through(state, response, resource, record_id))
            return ops
        if op in _BULK_OPS or op == "restore":
            ids = _ids_from_body(response)
            if ids is not None:
                ops = list(collections)
                for item in ids:
                    ops.append(_Clear(record_prefix(resource, item)))
                    ops.append(_Clear(revisions_prefix(resource, item)))
                return ops
        return [_Clear(prefix) for prefix in resource_prefixes(resource)]

    def _write_through(
        self, state: _State, response: httpx2.Response, resource: str, record_id: int
    ) -> list[_Op]:
        """Store the PATCH response as the record's entry (it is the read view plus ETag)."""
        if response.status_code != HTTPStatus.OK or not self._storable(response):
            return []
        fresh, stale = self._entry_times(state, "record")
        new = build_entry(
            response, now=self.clock(), fresh=fresh, stale=stale, keep_etag=state.policy.etag
        )
        if new.etag is None and fresh <= 0:
            return []
        key = build_key(
            resource, "record", record_id, state.ctx["scope"], api_version=state.api_version
        )
        return [_Set(key, new, self._retention(fresh, stale))]


def _split(ops: list[_Op]) -> tuple[list[str], list[_Set | _Delete]]:
    """The prefixes to clear (once, together) and the other ops, in order.

    Clears go first: a write-through `_Set` follows a clear of the same record.
    """
    prefixes = list(dict.fromkeys(op.prefix for op in ops if isinstance(op, _Clear)))
    return prefixes, [op for op in ops if not isinstance(op, _Clear)]


def _clear_many(core: _Core, store: CacheStore, prefixes: Sequence[str]) -> bool:
    """One pass over the store when it can (`clear_many`), else a clear per prefix.

    A store that reports a failed clear (returns ``False``) leaves its resources not fresh.
    """
    many = getattr(store, "clear_many", None)
    if many is not None:
        ok = many(prefixes) is not False
    else:
        results = [store.clear(prefix) for prefix in prefixes]  # every prefix, no short-circuit
        ok = False not in results
    if not ok:
        core.clear_failed(prefixes)
    return ok


async def _aclear_many(core: _Core, store: AsyncCacheStore, prefixes: Sequence[str]) -> bool:
    many = getattr(store, "clear_many", None)
    if many is not None:
        ok = await many(prefixes) is not False
    else:
        results = [await store.clear(prefix) for prefix in prefixes]
        ok = False not in results
    if not ok:
        core.clear_failed(prefixes)
    return ok


def _manifest_resources(body: bytes) -> set[str]:
    try:
        data = json.loads(body)
        return {r["name"] for r in data["resources"] if isinstance(r["name"], str)}
    except ValueError, KeyError, TypeError:
        return set()


def _ids_from_body(response: httpx2.Response) -> list[int] | None:
    """The record ids of a bulk response (``{matched, ids}``), or ``None`` if unreadable."""
    try:
        ids = response.json()["ids"]
        return [int(i) for i in ids]
    except ValueError, KeyError, TypeError:
        return None


def _stale_response(entry: CacheEntry, request: httpx2.Request, status: str) -> httpx2.Response:
    return to_response(entry, request, status=status, extensions={})


class CacheLayer:
    """Synchronous cache hook: a `TransportHook` over a `CacheStore`.

    ``policies`` maps manifest resource names (all versions) or ``(name, api_version)`` pairs to
    their policy, the pair winning; a resource without a policy (or with ``enabled=False``) is
    not cached. The manifest document is cached per caller scope.
    """

    def __init__(
        self,
        store: CacheStore,
        policies: Mapping[PolicyKey, CachePolicy],
        *,
        cache_manifest: bool = True,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Create the hook; ``clock`` (epoch seconds) is injectable for tests."""
        self._store = store
        self._core = _Core(policies, cache_manifest=cache_manifest, clock=clock)

    def on_request(self, request: httpx2.Request) -> httpx2.Response | None:
        """Serve a fresh hit, or add ``If-None-Match`` for a stored entry."""
        state = self._core.begin(request)
        if state is None:
            return None
        request.extensions[STATE_KEY] = state
        if state.key is None:
            return None
        state.entry = self._store.get(state.key)
        decision = self._core.decide(state, state.entry, swr=False)
        if decision == "hit":
            return _stale_response(cast("CacheEntry", state.entry), request, "hit")
        if decision == "revalidate":
            self._core.add_validator(request, state)
        return None

    def on_response(self, request: httpx2.Request, response: httpx2.Response) -> httpx2.Response:
        """Store, rebuild a ``304`` or invalidate after a write."""
        state: _State | None = request.extensions.get(STATE_KEY)
        if state is None:
            return response
        if self._core.wants_body(state, response):
            response.read()
        result, ops = self._core.plan(state, request, response)
        expected = self._core.finish(state, ops)
        prefixes, rest = _split(ops)
        if prefixes:
            _clear_many(self._core, self._store, prefixes)
        for write in rest:
            self._apply(write)
        for late in self._core.late_deletes(state, ops, expected):  # after the stores above
            self._apply(late)
        if result is not response:
            response.close()
        return result

    def _apply(self, op: _Set | _Delete) -> None:
        if isinstance(op, _Set):
            self._store.set(op.key, op.entry, op.ttl)
        else:
            self._store.delete(op.key)

    # -- control surface used by events and the client ------------------------------------

    def set_events_active(self, resource: str, active: bool) -> None:
        """Record whether the SSE stream of ``resource`` is connected (gates fresh hits)."""
        self._core.set_events_active(resource, active)

    def events_active(self, resource: str) -> bool:
        """Whether the SSE stream of ``resource`` is currently connected."""
        return self._core.events_active(resource)

    def invalidate_event(self, resource: str, action: str, record_id: int | None) -> None:
        """Clear what an SSE event makes stale (see `keys.event_prefixes`), in every version."""
        _clear_many(self._core, self._store, self._core.event_clears(resource, action, record_id))

    def clear_resource(self, resource: str) -> None:
        """Clear everything cached for ``resource`` except ``filters`` (after a reconnect)."""
        # A successful reset ends the not-fresh period (a failed one re-marked it).
        if _clear_many(self._core, self._store, self._core.reset_clears(resource)):
            self._core.cleared(resource)

    def clear(self, prefix: str | None = None) -> None:
        """Clear keys under ``prefix`` (everything the store owns when ``None``)."""
        if self._store.clear(prefix) is False:
            self._core.clear_failed([prefix])
        elif prefix is None:
            self._core.cleared(None)

    def cached_etag(
        self, resource: str, record_id: int, scope: str, api_version: str | None = None
    ) -> str | None:
        """The stored ETag of a record of one API version (``_`` if None), for ``auto_if_match``."""
        entry = self._store.get(self._core.etag_key(resource, record_id, scope, api_version))
        return None if entry is None else entry.etag


class AsyncCacheLayer:
    """Asynchronous cache hook: an `AsyncTransportHook` over an `AsyncCacheStore`, with SWR.

    Implements `BindsTransport`: once the client has called `bind`, a stale entry inside the
    policy's ``stale_while_revalidate`` window is returned at once and refreshed in a background
    task (one per key at a time) through the raw transport.
    """

    def __init__(
        self,
        store: AsyncCacheStore,
        policies: Mapping[PolicyKey, CachePolicy],
        *,
        cache_manifest: bool = True,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Create the hook; ``clock`` (epoch seconds) is injectable for tests."""
        self._store = store
        self._core = _Core(policies, cache_manifest=cache_manifest, clock=clock)
        self._send: SendRaw | None = None
        self._inflight: dict[str, asyncio.Task[None]] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def bind(self, send: SyncSendRaw | SendRaw) -> None:
        """Receive the raw send function (enables stale-while-revalidate)."""
        self._send = cast("SendRaw", send)

    async def aclose(self) -> None:
        """Cancel background revalidations and wait; no later hit schedules another (unbinds)."""
        self._send = None
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight.clear()

    async def on_request(self, request: httpx2.Request) -> httpx2.Response | None:
        """Serve a fresh or stale hit, or add ``If-None-Match`` for a stored entry."""
        state = self._core.begin(request)
        if state is None:
            return None
        request.extensions[STATE_KEY] = state
        if state.key is None:
            return None
        state.entry = await self._store.get(state.key)
        decision = self._core.decide(state, state.entry, swr=self._send is not None)
        entry = state.entry
        if decision == "hit":
            return _stale_response(cast("CacheEntry", entry), request, "hit")
        if decision == "stale":
            self._schedule(state, request)
            return _stale_response(cast("CacheEntry", entry), request, "stale")
        if decision == "revalidate":
            self._core.add_validator(request, state)
        return None

    async def on_response(
        self, request: httpx2.Request, response: httpx2.Response
    ) -> httpx2.Response:
        """Store, rebuild a ``304`` or invalidate after a write."""
        state: _State | None = request.extensions.get(STATE_KEY)
        if state is None:
            return response
        if self._core.wants_body(state, response):
            await response.aread()
        result, ops = self._core.plan(state, request, response)
        expected = self._core.finish(state, ops)
        prefixes, rest = _split(ops)
        if prefixes:
            await _aclear_many(self._core, self._store, prefixes)
        for write in rest:
            await self._apply(write)
        for late in self._core.late_deletes(state, ops, expected):  # after the stores above
            await self._apply(late)
        if result is not response:
            await response.aclose()
        return result

    async def _apply(self, op: _Set | _Delete) -> None:
        if isinstance(op, _Set):
            await self._store.set(op.key, op.entry, op.ttl)
        else:
            await self._store.delete(op.key)

    # -- stale-while-revalidate -------------------------------------------------------------

    def _schedule(self, state: _State, request: httpx2.Request) -> None:
        key = cast("str", state.key)
        if key in self._inflight:
            return
        task = asyncio.get_running_loop().create_task(self._revalidate(state, request))
        self._inflight[key] = task
        self._tasks.add(task)
        task.add_done_callback(lambda done: self._finished(key, done))

    def _finished(self, key: str, task: asyncio.Task[None]) -> None:
        """A revalidation ended, however (also cancelled before it ever started)."""
        self._tasks.discard(task)
        if self._inflight.get(key) is task:  # not a newer task for the same key
            del self._inflight[key]

    async def _revalidate(self, state: _State, request: httpx2.Request) -> None:
        key = cast("str", state.key)
        send = cast("SendRaw", self._send)
        try:
            fresh_request = httpx2.Request(
                "GET",
                request.url,
                headers=request.headers,
                extensions=dict(request.extensions),
            )
            fresh_state = replace(
                state,
                validated=False,
                generation=self._core.generation(state.ctx["resource"]),  # as of this request
            )
            self._core.add_validator(fresh_request, fresh_state)
            fresh_request.extensions[STATE_KEY] = fresh_state
            response = await send(fresh_request)
            result = await self.on_response(fresh_request, response)
            await result.aclose()
        except Exception:
            log.exception("background revalidation of %s failed", key)

    # -- control surface used by events and the client ------------------------------------

    def set_events_active(self, resource: str, active: bool) -> None:
        """Record whether the SSE stream of ``resource`` is connected (gates fresh hits)."""
        self._core.set_events_active(resource, active)

    def events_active(self, resource: str) -> bool:
        """Whether the SSE stream of ``resource`` is currently connected."""
        return self._core.events_active(resource)

    async def invalidate_event(self, resource: str, action: str, record_id: int | None) -> None:
        """Clear what an SSE event makes stale (see `keys.event_prefixes`), in every version."""
        await _aclear_many(
            self._core, self._store, self._core.event_clears(resource, action, record_id)
        )

    async def clear_resource(self, resource: str) -> None:
        """Clear everything cached for ``resource`` except ``filters`` (after a reconnect)."""
        if await _aclear_many(self._core, self._store, self._core.reset_clears(resource)):
            self._core.cleared(resource)

    async def clear(self, prefix: str | None = None) -> None:
        """Clear keys under ``prefix`` (everything the store owns when ``None``)."""
        if await self._store.clear(prefix) is False:
            self._core.clear_failed([prefix])
        elif prefix is None:
            self._core.cleared(None)

    async def cached_etag(
        self, resource: str, record_id: int, scope: str, api_version: str | None = None
    ) -> str | None:
        """The stored ETag of a record of one API version (``_`` if None), for ``auto_if_match``."""
        entry = await self._store.get(self._core.etag_key(resource, record_id, scope, api_version))
        return None if entry is None else entry.etag
