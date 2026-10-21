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
from collections.abc import Callable, Mapping
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
    kind_prefix,
    manifest_key,
    record_prefix,
    resource_prefix,
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
    entry: CacheEntry | None = None
    force_revalidate: bool = False
    validated: bool = False


class _Core:
    """Shared decisions of the sync and async layers; performs no I/O."""

    def __init__(
        self,
        policies: Mapping[str, CachePolicy],
        *,
        cache_manifest: bool,
        clock: Callable[[], float],
    ) -> None:
        self.policies = dict(policies)
        self.cache_manifest = cache_manifest
        self.clock = clock
        self._events_active: set[str] = set()
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
        """Before applying ``ops``: bump if a write clears; return the generation expected after."""
        if state.kind is None and any(isinstance(op, _Clear) for op in ops):
            return self.bump(state.ctx["resource"])
        return state.generation

    def late_deletes(self, state: _State, ops: list[_Op], expected: int) -> list[_Op]:
        """Deletes for the entries just stored if an invalidation landed while storing them."""
        if self.generation(state.ctx["resource"]) == expected:
            return []
        return [_Delete(op.key) for op in ops if isinstance(op, _Set)]

    # -- SSE gate ---------------------------------------------------------------------------

    def set_events_active(self, resource: str, active: bool) -> None:
        if active:
            self._events_active.add(resource)
        else:
            self._events_active.discard(resource)

    def events_active(self, resource: str) -> bool:
        return resource in self._events_active

    # -- request side -----------------------------------------------------------------------

    def begin(self, request: httpx2.Request) -> _State | None:
        ctx: RequestContext | None = request.extensions.get(EXTENSION_KEY)
        if ctx is None:
            return None
        resource = ctx["resource"]
        version = str(ctx.get("api_version") or UNVERSIONED)
        generation = self.generation(resource)
        if request.method != "GET":
            policy = self.policies.get(resource) if resource else None
            if policy is None or not policy.enabled:
                return None
            return _State(ctx, policy, None, None, version, generation)
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
        found = self.policies.get(resource) if resource else None
        if found is None or not found.enabled or op not in KINDS or resource is None:
            return None
        key = build_key(
            resource, op, ctx["record_id"], ctx["scope"], request.url.query, api_version=version
        )
        return found, op, key

    def _gate_open(self, policy: CachePolicy, resource: str | None, kind: str) -> bool:
        """Whether an entry of ``kind`` may be fresh (or stale-while-revalidate) right now."""
        if policy.revalidate_only or kind == "manifest":
            return False
        return kind in STATS_KINDS or (resource is not None and resource in self._events_active)

    def _times(self, policy: CachePolicy, resource: str | None, kind: str) -> tuple[float, float]:
        """Seconds fresh and seconds of stale-while-revalidate granted to a new entry."""
        if not self._gate_open(policy, resource, kind):
            return 0.0, 0.0
        fresh = policy.stats_ttl if kind in STATS_KINDS else policy.max_age
        return fresh, policy.stale_while_revalidate

    def _entry_times(self, state: _State, kind: str) -> tuple[float, float]:
        """`_times` for a response; none if an invalidation landed while it was in flight."""
        resource = state.ctx["resource"]
        if self.generation(resource) != state.generation:
            return 0.0, 0.0  # at most revalidate-only: the body may predate the invalidation
        return self._times(state.policy, resource, kind)

    @staticmethod
    def _retention(fresh: float, stale: float) -> float:
        return max(fresh + stale, _MIN_RETENTION)

    def decide(self, state: _State, entry: CacheEntry | None, *, swr: bool) -> _Decision:
        if entry is None:
            return "miss"
        resource = state.ctx["resource"]
        if self._gate_open(state.policy, resource, cast("str", state.kind)) and not (
            state.force_revalidate
        ):
            now = self.clock()
            if now < entry.fresh_until:
                return "hit"
            if swr and now < entry.stale_until:
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
        return status == HTTPStatus.OK and ctx["op"] == "update" and ctx["record_id"] is not None

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
                updated, request, status="revalidated", extensions=response.extensions
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
        pairs = {(name, UNVERSIONED) for name in self.policies}
        pairs |= _manifest_resources(old.body) | _manifest_resources(new.body)
        return [_Clear(kind_prefix(name, version, "filters")) for name, version in sorted(pairs)]

    def _write_ops(self, state: _State, response: httpx2.Response) -> list[_Op]:
        ctx = state.ctx
        resource = cast("str", ctx["resource"])
        record_id = ctx["record_id"]
        status = response.status_code
        if status == HTTPStatus.PRECONDITION_FAILED:
            if record_id is None:
                return []
            return [_Clear(record_prefix(resource, state.api_version, record_id))]
        if not HTTPStatus.OK <= status < HTTPStatus.MULTIPLE_CHOICES:
            return []
        # Any successful write: clear the resource in every API version (they share one event
        # stream, so a write is visible in all), then store a single PATCH as the record entry.
        ops: list[_Op] = [_Clear(resource_prefix(resource))]
        if ctx["op"] == "update" and record_id is not None:
            ops.extend(self._write_through(state, response, resource, record_id))
        return ops

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


def _manifest_resources(body: bytes) -> set[tuple[str, str]]:
    """The ``(name, api_version)`` pairs of a manifest document; empty if unreadable."""
    try:
        data = json.loads(body)
        return {
            (r["name"], r["api_version"])
            for r in data["resources"]
            if isinstance(r["name"], str) and isinstance(r["api_version"], str)
        }
    except ValueError, KeyError, TypeError:
        return set()


def _stale_response(entry: CacheEntry, request: httpx2.Request, status: str) -> httpx2.Response:
    return to_response(entry, request, status=status, extensions={})


class CacheLayer:
    """Synchronous cache hook: a `TransportHook` over a `CacheStore`.

    ``policies`` maps manifest resource names to their policy; a resource without a policy (or
    with ``enabled=False``) is not cached. The manifest document is cached per caller scope.
    """

    def __init__(
        self,
        store: CacheStore,
        policies: Mapping[str, CachePolicy],
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
        for op in ops:
            self._apply(op)
        for op in self._core.late_deletes(state, ops, expected):
            self._apply(op)
        if result is not response:
            response.close()
        return result

    def _apply(self, op: _Op) -> None:
        if isinstance(op, _Clear):
            self._store.clear(op.prefix)
        elif isinstance(op, _Set):
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
        """Clear what an SSE event makes stale: the whole resource, in every API version.

        ``action`` and ``record_id`` are accepted for the listener contract but unused: versions
        share one stream and a key cannot be cleared by kind across them. Responses in flight
        are stored revalidate-only afterwards.
        """
        del action, record_id
        self.clear_resource(resource)

    def clear_resource(self, resource: str) -> None:
        """Clear everything cached for ``resource`` in every API version (after a reconnect)."""
        self._core.bump(resource)
        self._store.clear(resource_prefix(resource))

    def clear(self, prefix: str | None = None) -> None:
        """Clear keys under ``prefix`` (everything the store owns when ``None``)."""
        self._store.clear(prefix)

    def cached_etag(
        self, resource: str, record_id: int, scope: str, api_version: str | None = None
    ) -> str | None:
        """The stored ETag of a record of one API version (``_`` if None), for ``auto_if_match``."""
        version = api_version or UNVERSIONED
        entry = self._store.get(
            build_key(resource, "record", record_id, scope, api_version=version)
        )
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
        policies: Mapping[str, CachePolicy],
        *,
        cache_manifest: bool = True,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Create the hook; ``clock`` (epoch seconds) is injectable for tests."""
        self._store = store
        self._core = _Core(policies, cache_manifest=cache_manifest, clock=clock)
        self._send: SendRaw | None = None
        self._inflight: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()

    def bind(self, send: SyncSendRaw | SendRaw) -> None:
        """Receive the raw send function (enables stale-while-revalidate)."""
        self._send = cast("SendRaw", send)

    async def aclose(self) -> None:
        """Cancel background revalidations and wait for them to finish."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

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
        for op in ops:
            await self._apply(op)
        for op in self._core.late_deletes(state, ops, expected):
            await self._apply(op)
        if result is not response:
            await response.aclose()
        return result

    async def _apply(self, op: _Op) -> None:
        if isinstance(op, _Clear):
            await self._store.clear(op.prefix)
        elif isinstance(op, _Set):
            await self._store.set(op.key, op.entry, op.ttl)
        else:
            await self._store.delete(op.key)

    # -- stale-while-revalidate -------------------------------------------------------------

    def _schedule(self, state: _State, request: httpx2.Request) -> None:
        key = cast("str", state.key)
        if key in self._inflight:
            return
        self._inflight.add(key)
        task = asyncio.get_running_loop().create_task(self._revalidate(state, request))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

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
        finally:
            self._inflight.discard(key)

    # -- control surface used by events and the client ------------------------------------

    def set_events_active(self, resource: str, active: bool) -> None:
        """Record whether the SSE stream of ``resource`` is connected (gates fresh hits)."""
        self._core.set_events_active(resource, active)

    def events_active(self, resource: str) -> bool:
        """Whether the SSE stream of ``resource`` is currently connected."""
        return self._core.events_active(resource)

    async def invalidate_event(self, resource: str, action: str, record_id: int | None) -> None:
        """Clear what an SSE event makes stale: the whole resource, in every API version.

        ``action`` and ``record_id`` are accepted for the listener contract but unused: versions
        share one stream and a key cannot be cleared by kind across them. Responses in flight
        are stored revalidate-only afterwards.
        """
        del action, record_id
        await self.clear_resource(resource)

    async def clear_resource(self, resource: str) -> None:
        """Clear everything cached for ``resource`` in every API version (after a reconnect)."""
        self._core.bump(resource)
        await self._store.clear(resource_prefix(resource))

    async def clear(self, prefix: str | None = None) -> None:
        """Clear keys under ``prefix`` (everything the store owns when ``None``)."""
        await self._store.clear(prefix)

    async def cached_etag(
        self, resource: str, record_id: int, scope: str, api_version: str | None = None
    ) -> str | None:
        """The stored ETag of a record of one API version (``_`` if None), for ``auto_if_match``."""
        version = api_version or UNVERSIONED
        entry = await self._store.get(
            build_key(resource, "record", record_id, scope, api_version=version)
        )
        return None if entry is None else entry.etag
