"""Sync and async CRUD clients: manifest-driven resource handles over hooked httpx2 transports."""

from __future__ import annotations

import builtins
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self, TypedDict, Unpack, cast

import httpx2

from craftainer_crud_sdk.auth import (
    BearerAuth,
    acall_provider,
    authorization_scope,
    call_provider,
    sent_authorization,
    stamp_scope,
)
from craftainer_crud_sdk.errors import (
    CapabilityError,
    ConnectionFailed,
    InvalidQueryError,
    ResponseError,
    UnknownResourceError,
    problem_from_response,
)
from craftainer_crud_sdk.interfaces import (
    EXTENSION_KEY,
    AsyncTokenProvider,
    AsyncTransportHook,
    BindsTransport,
    Op,
    RequestContext,
    TokenProvider,
    TransportHook,
)
from craftainer_crud_sdk.manifest import Manifest, Resource, aload_manifest, load_manifest
from craftainer_crud_sdk.origin import check_origins, join_same_origin
from craftainer_crud_sdk.query import build_query, encode_params

__all__ = [
    "AsyncCrudClient",
    "AsyncHookedTransport",
    "AsyncResourceClient",
    "CrudClient",
    "HookedTransport",
    "JsonList",
    "JsonObject",
    "ResourceClient",
]

_TIMEOUT = httpx2.Timeout(5.0)

type JsonObject = dict[str, Any]
type JsonList = builtins.list[JsonObject]
type Filters = Mapping[str, object]
type Sort = str | Sequence[str]


class HookedTransport(httpx2.BaseTransport):
    """Runs `TransportHook`s around a sync transport.

    `on_request` runs in registration order and the first non-`None` result short-circuits the
    network. `on_response` runs in reverse, for the hooks whose `on_request` ran before the
    short-circuiting one (all of them for a network response). Before any hook runs, the request
    context's cache scope is set from the Authorization header the request actually carries.
    """

    def __init__(
        self,
        inner: httpx2.BaseTransport,
        hooks: Sequence[TransportHook],
        *,
        auth: httpx2.Auth | None = None,
    ) -> None:
        """Wrap `inner`; hooks that implement `BindsTransport` receive `send_authenticated`.

        `auth` is the client's auth flow, which only `send_authenticated` runs (the client's own
        requests get it from the `httpx2.Client`).
        """
        self._inner = inner
        self._hooks = list(hooks)
        self._auth = auth
        for hook in self._hooks:
            self._bind(hook)

    def _bind(self, hook: TransportHook) -> None:
        if isinstance(hook, BindsTransport):
            hook.bind(self.send_authenticated)

    @property
    def hooks(self) -> tuple[TransportHook, ...]:
        """The installed hooks, in order."""
        return tuple(self._hooks)

    def add_hook(self, hook: TransportHook) -> None:
        """Install `hook` after the existing ones (it is bound like a constructor hook)."""
        self._hooks.append(hook)
        self._bind(hook)

    def send_raw(self, request: httpx2.Request) -> httpx2.Response:
        """Send past all hooks."""
        return self._inner.handle_request(request)

    def send_authenticated(self, request: httpx2.Request) -> httpx2.Response:
        """Send past all hooks, but through the auth flow (token, one refresh-and-retry on 401).

        For a hook's own background requests: they must not re-enter the hooks, yet authenticate
        like any request: the provider's token replaces an ``Authorization`` header the request
        already carries (so a copied request never replays a stale token), and without a token the
        header stays as it is.
        """
        if self._auth is None:
            stamp_scope(request)
            return self.send_raw(request)
        flow = self._auth.sync_auth_flow(request)
        try:
            current = next(flow)
            while True:
                response = self.send_raw(current)
                try:
                    current = flow.send(response)
                except StopIteration:
                    return response
                except BaseException:
                    response.close()  # e.g. the provider failed to refresh
                    raise
                response.close()  # the retry replaces it
        finally:
            flow.close()

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        """Run the hooks around the inner transport."""
        stamp_scope(request)  # the scope of the Authorization header actually on the wire
        ran: list[TransportHook] = []
        response: httpx2.Response | None = None
        for hook in self._hooks:
            response = hook.on_request(request)
            if response is not None:
                break
            ran.append(hook)
        if response is None:
            response = self.send_raw(request)
        for hook in reversed(ran):
            response = hook.on_response(request, response)
        return response

    def close(self) -> None:
        """Close the inner transport."""
        self._inner.close()


class AsyncHookedTransport(httpx2.AsyncBaseTransport):
    """Async twin of `HookedTransport`."""

    def __init__(
        self,
        inner: httpx2.AsyncBaseTransport,
        hooks: Sequence[AsyncTransportHook],
        *,
        auth: httpx2.Auth | None = None,
    ) -> None:
        """Wrap `inner`; hooks that implement `BindsTransport` receive `send_authenticated`."""
        self._inner = inner
        self._hooks = list(hooks)
        self._auth = auth
        for hook in self._hooks:
            self._bind(hook)

    def _bind(self, hook: AsyncTransportHook) -> None:
        if isinstance(hook, BindsTransport):
            hook.bind(self.send_authenticated)

    @property
    def hooks(self) -> tuple[AsyncTransportHook, ...]:
        """The installed hooks, in order."""
        return tuple(self._hooks)

    def add_hook(self, hook: AsyncTransportHook) -> None:
        """Install `hook` after the existing ones (it is bound like a constructor hook)."""
        self._hooks.append(hook)
        self._bind(hook)

    async def send_raw(self, request: httpx2.Request) -> httpx2.Response:
        """Send past all hooks."""
        return await self._inner.handle_async_request(request)

    async def send_authenticated(self, request: httpx2.Request) -> httpx2.Response:
        """Async twin of `HookedTransport.send_authenticated`."""
        if self._auth is None:
            stamp_scope(request)
            return await self.send_raw(request)
        flow = self._auth.async_auth_flow(request)
        try:
            current = await anext(flow)
            while True:
                response = await self.send_raw(current)
                try:
                    current = await flow.asend(response)
                except StopAsyncIteration:
                    return response
                except BaseException:
                    await response.aclose()  # e.g. the provider failed to refresh
                    raise
                await response.aclose()  # the retry replaces it
        finally:
            await flow.aclose()

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        """Run the hooks around the inner transport."""
        stamp_scope(request)
        ran: list[AsyncTransportHook] = []
        response: httpx2.Response | None = None
        for hook in self._hooks:
            response = await hook.on_request(request)
            if response is not None:
                break
            ran.append(hook)
        if response is None:
            response = await self.send_raw(request)
        for hook in reversed(ran):
            response = await hook.on_response(request, response)
        return response

    async def aclose(self) -> None:
        """Close the inner transport."""
        await self._inner.aclose()


@dataclass(frozen=True, slots=True)
class _Call:
    method: str
    url: str
    context: RequestContext
    json: object | None = None
    headers: Mapping[str, str] | None = None
    auto_if_match: bool = False  # `If-Match` is looked up after authentication, if a lookup is set


_AUTO_IF_MATCH = "craftainer_auto_if_match"
"""Request extension: the request wants the looked-up ETag as `If-Match` (set by `_build`)."""


def _build(http: httpx2.Client | httpx2.AsyncClient, call: _Call) -> httpx2.Request:
    headers = {"Accept": "application/json", **(call.headers or {})}
    # `build_request` keeps the context dict by reference, so the transport can fill the scope.
    extensions: dict[str, object] = {EXTENSION_KEY: call.context}
    if call.auto_if_match:
        extensions[_AUTO_IF_MATCH] = True
    return http.build_request(
        call.method, call.url, json=call.json, headers=headers, extensions=extensions
    )


class _IfMatchHook:
    """Sync transport hook: sets `If-Match` from the etag lookup once the request is authenticated.

    It runs below the auth flow, so the scope it looks up is the one of the very token the request
    carries (one provider call per request; a retry after a 401 looks up again).
    """

    def __init__(self) -> None:
        self.lookup: Callable[[str, int, str, str | None], str | None] | None = None

    def on_request(self, request: httpx2.Request) -> httpx2.Response | None:
        if self.lookup is not None and request.extensions.get(_AUTO_IF_MATCH):
            ctx = cast("RequestContext", request.extensions[EXTENSION_KEY])
            etag = self.lookup(
                cast("str", ctx["resource"]),
                cast("int", ctx["record_id"]),
                ctx["scope"],
                ctx["api_version"],
            )
            request.headers.pop("If-Match", None)
            if etag is not None:
                request.headers["If-Match"] = etag
        return None

    def on_response(self, request: httpx2.Request, response: httpx2.Response) -> httpx2.Response:
        return response


class _AsyncIfMatchHook:
    """Async twin of `_IfMatchHook`."""

    def __init__(self) -> None:
        self.lookup: Callable[[str, int, str, str | None], Awaitable[str | None]] | None = None

    async def on_request(self, request: httpx2.Request) -> httpx2.Response | None:
        if self.lookup is not None and request.extensions.get(_AUTO_IF_MATCH):
            ctx = cast("RequestContext", request.extensions[EXTENSION_KEY])
            etag = await self.lookup(
                cast("str", ctx["resource"]),
                cast("int", ctx["record_id"]),
                ctx["scope"],
                ctx["api_version"],
            )
            request.headers.pop("If-Match", None)
            if etag is not None:
                request.headers["If-Match"] = etag
        return None

    async def on_response(
        self, request: httpx2.Request, response: httpx2.Response
    ) -> httpx2.Response:
        return response


def _checked(response: httpx2.Response) -> httpx2.Response:
    if response.status_code >= 400:  # noqa: PLR2004
        raise problem_from_response(response)
    if (
        httpx2.codes.MULTIPLE_CHOICES <= response.status_code < httpx2.codes.BAD_REQUEST
        and response.status_code != httpx2.codes.NOT_MODIFIED
    ):
        location = response.headers.get("location")
        raise ResponseError(
            f"unexpected {response.status_code} redirect"
            + (f" to {location!r}" if location else "")
            + "; the client never follows redirects",
            response,
        )
    return response


def _page_size(resource: Resource, limit: int | None) -> int:
    """The `iter_all` page size: the caller's, else the default capped at the server maximum."""
    pagination = resource.pagination
    return min(pagination.default_limit, pagination.max_limit) if limit is None else limit


def _decode(response: httpx2.Response) -> object:
    if response.status_code == httpx2.codes.NO_CONTENT or not response.content:
        return None
    try:
        return response.json()
    except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError
        raise ResponseError(
            f"{response.status_code} response body is not valid JSON: {exc}", response
        ) from exc


def _decode_object(response: httpx2.Response) -> JsonObject:
    """Decode a body that must be a JSON object.

    An empty body, a ``204`` or another JSON shape is a `ResponseError`, not a ``None`` or a
    wrongly shaped value handed back under a typed return.
    """
    body = _decode(response)
    if not isinstance(body, dict):
        raise ResponseError(
            f"{response.status_code} response: expected a JSON object, got {_shape(body)}", response
        )
    return body


def _decode_list(response: httpx2.Response) -> JsonList:
    """Decode a body that must be a JSON array (see `_decode_object`)."""
    body = _decode(response)
    if not isinstance(body, list):
        raise ResponseError(
            f"{response.status_code} response: expected a JSON array, got {_shape(body)}", response
        )
    return body


def _shape(body: object) -> str:
    return "no body" if body is None else type(body).__name__


class _QueryArgs(TypedDict, total=False):
    record_id: int | None
    filters: Mapping[str, object] | None
    sort: str | Sequence[str] | None
    skip: int | None
    limit: int | None
    include_archived: bool | None
    only_archived: bool
    include_unpublished: bool | None


class _Plan:
    """Builds the requests of one resource; shared by the sync and async handles."""

    def __init__(
        self,
        base_url: str,
        resource: Resource,
        allowed_origins: Sequence[str] | None = None,
    ) -> None:
        self._base_url = base_url
        self._allowed_origins = allowed_origins
        self.resource = resource

    def _call(  # noqa: PLR0913
        self,
        method: str,
        op: Op,
        path: str,
        query: str = "",
        *,
        record_id: int | None = None,
        json: object | None = None,
        if_match: str | None = None,
    ) -> _Call:
        url = join_same_origin(self._base_url, path, self._allowed_origins) + (
            f"?{query}" if query else ""
        )
        context = RequestContext(
            resource=self.resource.name,
            op=op,
            record_id=record_id,
            api_version=self.resource.api_version,
            scope="anon",
        )
        headers = {"If-Match": if_match} if if_match is not None else None
        auto = if_match is None and op in {"update", "delete"}
        return _Call(method, url, context, json, headers, auto_if_match=auto)

    def _query(self, **kwargs: Unpack[_QueryArgs]) -> str:
        paging = self.resource.pagination
        return build_query(
            self.resource.fields,
            max_limit=paging.max_limit,
            skip_param=paging.skip_param,
            limit_param=paging.limit_param,
            sort_param=paging.sort_param,
            **kwargs,
        )

    def _sub_path(self, key: str, supported: bool) -> str:
        path = self.resource.paths.get(key)
        if not supported or path is None:
            raise CapabilityError(f"resource {self.resource.name!r} does not support {key}")
        return path

    def _need(self, supported: bool, what: str) -> None:
        if not supported:
            raise CapabilityError(f"resource {self.resource.name!r} does not support {what}")

    def _bulk_filters(self, filters: Filters) -> str:
        if not filters:
            raise InvalidQueryError("a bulk operation needs at least one filter")
        return self._query(filters=filters)

    def events_url(self) -> str:
        """Absolute URL of the resource's SSE stream."""
        events = self.resource.capabilities.events
        if events is None:
            raise CapabilityError(f"resource {self.resource.name!r} does not support events")
        return join_same_origin(self._base_url, events.path, self._allowed_origins)

    def list_call(self, **kwargs: Unpack[_QueryArgs]) -> _Call:
        return self._call("GET", "list", self.resource.base_path, self._query(**kwargs))

    def get_call(self, record_id: int) -> _Call:
        query = self._query(record_id=record_id)
        return self._call("GET", "record", self.resource.base_path, query, record_id=record_id)

    def create_call(self, data: object) -> _Call:
        return self._call("POST", "create", self.resource.base_path, json=data)

    def update_call(self, record_id: int, data: object, if_match: str | None) -> _Call:
        query = self._query(record_id=record_id)
        return self._call(
            "PATCH", "update", self.resource.base_path, query,
            record_id=record_id, json=data, if_match=if_match,
        )  # fmt: skip

    def update_many_call(self, filters: Filters, data: object) -> _Call:
        self._need(self.resource.capabilities.bulk_update, "bulk_update")
        query = self._bulk_filters(filters)
        return self._call("PATCH", "bulk_update", self.resource.base_path, query, json=data)

    def delete_call(self, record_id: int, if_match: str | None) -> _Call:
        query = self._query(record_id=record_id)
        return self._call(
            "DELETE", "delete", self.resource.base_path, query,
            record_id=record_id, if_match=if_match,
        )  # fmt: skip

    def delete_many_call(self, filters: Filters) -> _Call:
        self._need(self.resource.capabilities.bulk_delete, "bulk_delete")
        query = self._bulk_filters(filters)
        return self._call("DELETE", "bulk_delete", self.resource.base_path, query)

    def restore_call(self, record_id: int | None, filters: Filters | None) -> _Call:
        path = self._sub_path("restore", self.resource.capabilities.archive_restore)
        if (record_id is None) == (filters is None):
            raise InvalidQueryError("restore takes exactly one of record_id and filters")
        query = (
            self._bulk_filters(filters) if filters is not None else self._query(record_id=record_id)
        )
        return self._call("POST", "restore", path, query, record_id=record_id)

    def stats_call(
        self, bucket: str | None, include_archived: bool | None, include_unpublished: bool | None
    ) -> _Call:
        path = self._sub_path("stats", self.resource.capabilities.stats)
        pairs = [("bucket", bucket)] if bucket is not None else []
        pairs += _flags(include_archived, include_unpublished)
        return self._call("GET", "stats", path, encode_params(pairs))

    def predict_call(self, field: str | None, periods: int | None, bucket: str | None) -> _Call:
        path = self._sub_path("predict", self.resource.capabilities.predict)
        pairs: list[tuple[str, str]] = []
        if field is not None:
            spec = self.resource.field(field)
            if spec is None or not spec.numeric:
                raise InvalidQueryError(f"cannot predict {field!r}: not a numeric field")
            pairs.append(("field", field))
        if periods is not None:
            if isinstance(periods, bool) or periods < 1:
                raise InvalidQueryError("periods must be an integer >= 1")
            pairs.append(("periods", str(periods)))
        if bucket is not None:
            pairs.append(("bucket", bucket))
        return self._call("GET", "predict", path, encode_params(pairs))

    def clone_call(self, record_id: int) -> _Call:
        path = self._sub_path("clone", self.resource.capabilities.clone)
        query = self._query(record_id=record_id)
        return self._call("POST", "clone", path, query, record_id=record_id)

    def draft_call(self, data: object) -> _Call:
        path = self._sub_path("draft", self.resource.capabilities.draft_publish)
        return self._call("POST", "draft", path, json=data)

    def publish_call(self, record_id: int) -> _Call:
        path = self._sub_path("publish", self.resource.capabilities.draft_publish)
        query = self._query(record_id=record_id)
        return self._call("POST", "publish", path, query, record_id=record_id)

    def filters_call(self) -> _Call:
        return self._call("GET", "filters", self._sub_path("filters", True))

    def revisions_call(self, record_id: int) -> _Call:
        path = self._sub_path("revisions", self.resource.capabilities.revisions)
        return self._call(
            "GET", "revisions", path, self._query(record_id=record_id), record_id=record_id
        )


def _flags(archived: bool | None, unpublished: bool | None) -> list[tuple[str, str]]:
    pairs = [("include_archived", archived), ("include_unpublished", unpublished)]
    return [(k, "true" if v else "false") for k, v in pairs if v is not None]


def _lookup(manifest: Manifest, name: str, api_version: str | None) -> Resource:
    resource = manifest.find(name, api_version)
    if resource is None:
        raise UnknownResourceError(
            f"the manifest has no resource {name!r}"
            + (f" {api_version}" if api_version else "")
            + " for this caller"
        )
    return resource


class ResourceClient(_Plan):
    """Synchronous operations on one resource."""

    def __init__(self, client: CrudClient, resource: Resource) -> None:
        """Bind to `client`; get one from `CrudClient.resource`."""
        super().__init__(client.base_url, resource, client.allowed_origins)
        self._client = client

    def list(  # noqa: PLR0913
        self,
        filters: Filters | None = None,
        sort: Sort | None = None,
        *,
        skip: int | None = None,
        limit: int | None = None,
        include_archived: bool | None = None,
        only_archived: bool = False,
        include_unpublished: bool | None = None,
    ) -> JsonList:
        """One page of records."""
        call = self.list_call(
            filters=filters, sort=sort, skip=skip, limit=limit,
            include_archived=include_archived, only_archived=only_archived,
            include_unpublished=include_unpublished,
        )  # fmt: skip
        return self._client.send_list(call)

    def iter_all(  # noqa: PLR0913
        self,
        filters: Filters | None = None,
        sort: Sort | None = None,
        *,
        skip: int = 0,
        limit: int | None = None,
        include_archived: bool | None = None,
        only_archived: bool = False,
        include_unpublished: bool | None = None,
    ) -> Iterator[JsonObject]:
        """Every record, paging with `skip += limit` until a page shorter than `limit`."""
        size = _page_size(self.resource, limit)
        while True:
            page = self.list(
                filters,
                sort,
                skip=skip,
                limit=size,
                include_archived=include_archived,
                only_archived=only_archived,
                include_unpublished=include_unpublished,
            )
            yield from page
            if len(page) < size:
                return
            skip += size

    def get(self, record_id: int) -> JsonObject:
        """One record."""
        return self._client.send_object(self.get_call(record_id))

    def create(self, data: Mapping[str, Any]) -> JsonObject:
        """Create a record."""
        return self._client.send_object(self.create_call(data))

    def update(
        self, record_id: int, data: Mapping[str, Any], if_match: str | None = None
    ) -> JsonObject:
        """Patch one record; `if_match` is an ETag (`412` when stale).

        Without `if_match`, a client with `auto_if_match` sends the cached record ETag, if any
        (looked up for the very token the request is sent with).
        """
        return self._client.send_object(self.update_call(record_id, data, if_match))

    def update_many(self, filters: Filters, data: Mapping[str, Any]) -> JsonObject:
        """Patch every record matching `filters`; returns `{matched, ids}`."""
        return self._client.send_object(self.update_many_call(filters, data))

    def delete(self, record_id: int, if_match: str | None = None) -> None:
        """Delete one record; `if_match` as for `update`."""
        self._client.send_none(self.delete_call(record_id, if_match))

    def delete_many(self, filters: Filters) -> JsonObject:
        """Delete every record matching `filters`; returns `{matched, ids}`."""
        return self._client.send_object(self.delete_many_call(filters))

    def restore(self, record_id: int | None = None, filters: Filters | None = None) -> JsonObject:
        """Restore archived records, by id or by filters."""
        return self._client.send_object(self.restore_call(record_id, filters))

    def stats(
        self,
        bucket: str | None = None,
        include_archived: bool | None = None,
        include_unpublished: bool | None = None,
    ) -> JsonObject:
        """Aggregates over the resource."""
        call = self.stats_call(bucket, include_archived, include_unpublished)
        return self._client.send_object(call)

    def predict(
        self, field: str | None = None, periods: int | None = None, bucket: str | None = None
    ) -> JsonObject:
        """A forecast."""
        return self._client.send_object(self.predict_call(field, periods, bucket))

    def clone(self, record_id: int) -> JsonObject:
        """Copy a record; returns the copy."""
        return self._client.send_object(self.clone_call(record_id))

    def create_draft(self, data: Mapping[str, Any]) -> JsonObject:
        """Create a draft (needs `draft_publish`)."""
        return self._client.send_object(self.draft_call(data))

    def publish(self, record_id: int) -> JsonObject:
        """Publish a draft (needs `draft_publish`)."""
        return self._client.send_object(self.publish_call(record_id))

    def filters(self) -> JsonList:
        """The server's filter metadata (`kind` instead of `value_type`)."""
        return self._client.send_list(self.filters_call())

    def revisions(self, record_id: int) -> JsonList:
        """Revisions of one record, newest first."""
        return self._client.send_list(self.revisions_call(record_id))


class CrudClient:
    """Synchronous client: discovers the manifest, hands out `ResourceClient`s."""

    def __init__(  # noqa: PLR0913
        self,
        base_url: str,
        *,
        token_provider: TokenProvider | None = None,
        hooks: Sequence[TransportHook] = (),
        transport: httpx2.BaseTransport | None = None,
        manifest_url: str | None = None,
        timeout: httpx2.Timeout | float | None = _TIMEOUT,
        headers: Mapping[str, str] | None = None,
        allowed_origins: Sequence[str] | None = None,
    ) -> None:
        """`base_url` is the server origin (or any URL on it); `manifest_url` skips discovery.

        The client builds its own `httpx2.Client` around `transport` (default: a plain
        `httpx2.HTTPTransport`, which does not read proxy environment variables), wrapped in a
        `HookedTransport` that runs `hooks`.

        Every URL the client builds from the manifest (request paths, the events stream, the
        catalog's `describedby` link) must be on `base_url`'s origin, or the join raises
        `ManifestError`, so the bearer token never goes to another host. `allowed_origins`
        (for example `["https://events.example.com"]`) opts further origins in. An explicit
        `manifest_url` is trusted as given.
        """
        self.base_url = base_url
        self.allowed_origins: tuple[str, ...] = check_origins(allowed_origins)
        self._manifest_url = manifest_url
        self._manifest: Manifest | None = None
        self._token_provider = token_provider
        auth = BearerAuth(token_provider) if token_provider is not None else None
        self._transport = HookedTransport(
            transport or httpx2.HTTPTransport(),
            hooks,
            auth=auth,
        )
        self._http = httpx2.Client(
            transport=self._transport,
            auth=auth,
            timeout=timeout,
            headers=headers,
        )
        self._if_match = _IfMatchHook()
        self._closers: list[tuple[Callable[[], None], Callable[[], None]]] = []

    @property
    def http(self) -> httpx2.Client:
        """The authenticated `httpx2.Client` every request goes through (also for SSE streams)."""
        return self._http

    @property
    def hooks(self) -> tuple[TransportHook, ...]:
        """The installed transport hooks."""
        return self._transport.hooks

    def add_hook(self, hook: TransportHook) -> None:
        """Install a transport hook after construction (it sees every later request)."""
        self._transport.add_hook(hook)

    def scope(self) -> str:
        """The cache scope of the current caller: that of the Authorization header a request gets.

        Same logic as the transport: the provider's token if it returns one, else the header
        configured on the client (`auth.sent_authorization`).
        """
        provider = self._token_provider
        token = None if provider is None else call_provider(provider)
        return authorization_scope(
            sent_authorization(token, self._http.headers.get("Authorization"))
        )

    def set_etag_lookup(
        self, lookup: Callable[[str, int, str, str | None], str | None] | None
    ) -> None:
        """Make `update`/`delete` send `lookup(resource, id, scope, version)` as `If-Match`.

        The lookup runs once per request, after authentication, with the scope of the token the
        request is actually sent with.
        """
        if lookup is not None and self._if_match not in self._transport.hooks:
            self._transport.add_hook(self._if_match)
        self._if_match.lookup = lookup

    def add_closer(self, close: Callable[[], None], join: Callable[[], None]) -> None:
        """Run `close` when the client closes, then close HTTP, then `join` (wait for threads)."""
        self._closers.append((close, join))

    def manifest(self, *, refresh: bool = False) -> Manifest:
        """The manifest for this caller, loaded once (`refresh=True` reloads)."""
        if self._manifest is None or refresh:
            self._manifest = load_manifest(
                self._http,
                self.base_url,
                manifest_url=self._manifest_url,
                allowed_origins=self.allowed_origins,
            )
        return self._manifest

    def resource(self, name: str, api_version: str | None = None) -> ResourceClient:
        """A handle for `name` (the highest `api_version` unless given)."""
        return ResourceClient(self, _lookup(self.manifest(), name, api_version))

    def _send(self, call: _Call) -> httpx2.Response:
        request = _build(self._http, call)
        try:
            response = self._http.send(request)
        except httpx2.RequestError as exc:
            raise ConnectionFailed(str(exc)) from exc
        return _checked(response)

    def send_object(self, call: _Call) -> JsonObject:
        """Internal: run a call that returns a JSON object."""
        return _decode_object(self._send(call))

    def send_list(self, call: _Call) -> JsonList:
        """Internal: run a call that returns a JSON array."""
        return _decode_list(self._send(call))

    def send_none(self, call: _Call) -> None:
        """Internal: run a call whose body is ignored."""
        self._send(call)

    def close(self) -> None:
        """Stop what `add_closer` registered, close the HTTP client and its transport."""
        closers, self._closers = self._closers[::-1], []
        errors: list[Exception] = []
        calls = [close for close, _ in closers]
        calls.append(self._http.close)
        calls.extend(join for _, join in closers)
        for call in calls:  # every step runs; the first failure is raised at the end
            try:
                call()
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise errors[0]

    def __enter__(self) -> Self:
        """Enter the context manager."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close on exit."""
        self.close()


class AsyncResourceClient(_Plan):
    """Asynchronous operations on one resource."""

    def __init__(self, client: AsyncCrudClient, resource: Resource) -> None:
        """Bind to `client`; get one from `AsyncCrudClient.resource`."""
        super().__init__(client.base_url, resource, client.allowed_origins)
        self._client = client

    async def list(  # noqa: PLR0913
        self,
        filters: Filters | None = None,
        sort: Sort | None = None,
        *,
        skip: int | None = None,
        limit: int | None = None,
        include_archived: bool | None = None,
        only_archived: bool = False,
        include_unpublished: bool | None = None,
    ) -> JsonList:
        """One page of records."""
        call = self.list_call(
            filters=filters, sort=sort, skip=skip, limit=limit,
            include_archived=include_archived, only_archived=only_archived,
            include_unpublished=include_unpublished,
        )  # fmt: skip
        return await self._client.send_list(call)

    async def iter_all(  # noqa: PLR0913
        self,
        filters: Filters | None = None,
        sort: Sort | None = None,
        *,
        skip: int = 0,
        limit: int | None = None,
        include_archived: bool | None = None,
        only_archived: bool = False,
        include_unpublished: bool | None = None,
    ) -> AsyncIterator[JsonObject]:
        """Every record, paging with `skip += limit` until a page shorter than `limit`."""
        size = _page_size(self.resource, limit)
        while True:
            page = await self.list(
                filters,
                sort,
                skip=skip,
                limit=size,
                include_archived=include_archived,
                only_archived=only_archived,
                include_unpublished=include_unpublished,
            )
            for record in page:
                yield record
            if len(page) < size:
                return
            skip += size

    async def get(self, record_id: int) -> JsonObject:
        """One record."""
        return await self._client.send_object(self.get_call(record_id))

    async def create(self, data: Mapping[str, Any]) -> JsonObject:
        """Create a record."""
        return await self._client.send_object(self.create_call(data))

    async def update(
        self, record_id: int, data: Mapping[str, Any], if_match: str | None = None
    ) -> JsonObject:
        """Patch one record; `if_match` is an ETag (`412` when stale).

        Without `if_match`, a client with `auto_if_match` sends the cached record ETag, if any
        (looked up for the very token the request is sent with).
        """
        return await self._client.send_object(self.update_call(record_id, data, if_match))

    async def update_many(self, filters: Filters, data: Mapping[str, Any]) -> JsonObject:
        """Patch every record matching `filters`; returns `{matched, ids}`."""
        return await self._client.send_object(self.update_many_call(filters, data))

    async def delete(self, record_id: int, if_match: str | None = None) -> None:
        """Delete one record; `if_match` as for `update`."""
        await self._client.send_none(self.delete_call(record_id, if_match))

    async def delete_many(self, filters: Filters) -> JsonObject:
        """Delete every record matching `filters`; returns `{matched, ids}`."""
        return await self._client.send_object(self.delete_many_call(filters))

    async def restore(
        self, record_id: int | None = None, filters: Filters | None = None
    ) -> JsonObject:
        """Restore archived records, by id or by filters."""
        return await self._client.send_object(self.restore_call(record_id, filters))

    async def stats(
        self,
        bucket: str | None = None,
        include_archived: bool | None = None,
        include_unpublished: bool | None = None,
    ) -> JsonObject:
        """Aggregates over the resource."""
        call = self.stats_call(bucket, include_archived, include_unpublished)
        return await self._client.send_object(call)

    async def predict(
        self, field: str | None = None, periods: int | None = None, bucket: str | None = None
    ) -> JsonObject:
        """A forecast."""
        return await self._client.send_object(self.predict_call(field, periods, bucket))

    async def clone(self, record_id: int) -> JsonObject:
        """Copy a record; returns the copy."""
        return await self._client.send_object(self.clone_call(record_id))

    async def create_draft(self, data: Mapping[str, Any]) -> JsonObject:
        """Create a draft (needs `draft_publish`)."""
        return await self._client.send_object(self.draft_call(data))

    async def publish(self, record_id: int) -> JsonObject:
        """Publish a draft (needs `draft_publish`)."""
        return await self._client.send_object(self.publish_call(record_id))

    async def filters(self) -> JsonList:
        """The server's filter metadata (`kind` instead of `value_type`)."""
        return await self._client.send_list(self.filters_call())

    async def revisions(self, record_id: int) -> JsonList:
        """Revisions of one record, newest first."""
        return await self._client.send_list(self.revisions_call(record_id))


class AsyncCrudClient:
    """Asynchronous client: discovers the manifest, hands out `AsyncResourceClient`s."""

    def __init__(  # noqa: PLR0913
        self,
        base_url: str,
        *,
        token_provider: TokenProvider | AsyncTokenProvider | None = None,
        hooks: Sequence[AsyncTransportHook] = (),
        transport: httpx2.AsyncBaseTransport | None = None,
        manifest_url: str | None = None,
        timeout: httpx2.Timeout | float | None = _TIMEOUT,
        headers: Mapping[str, str] | None = None,
        allowed_origins: Sequence[str] | None = None,
    ) -> None:
        """As `CrudClient` (also `allowed_origins`); the token provider may be sync or async."""
        self.base_url = base_url
        self.allowed_origins: tuple[str, ...] = check_origins(allowed_origins)
        self._manifest_url = manifest_url
        self._manifest: Manifest | None = None
        self._token_provider = token_provider
        auth = BearerAuth(token_provider) if token_provider is not None else None
        self._transport = AsyncHookedTransport(
            transport or httpx2.AsyncHTTPTransport(),
            hooks,
            auth=auth,
        )
        self._http = httpx2.AsyncClient(
            transport=self._transport,
            auth=auth,
            timeout=timeout,
            headers=headers,
        )
        self._if_match = _AsyncIfMatchHook()
        self._closers: list[Callable[[], Awaitable[None]]] = []

    @property
    def http(self) -> httpx2.AsyncClient:
        """The authenticated `httpx2.AsyncClient` every request goes through (also for SSE)."""
        return self._http

    @property
    def hooks(self) -> tuple[AsyncTransportHook, ...]:
        """The installed transport hooks."""
        return self._transport.hooks

    def add_hook(self, hook: AsyncTransportHook) -> None:
        """Install a transport hook after construction (it sees every later request)."""
        self._transport.add_hook(hook)

    async def scope(self) -> str:
        """The cache scope of the current caller; the same logic as `CrudClient.scope`."""
        provider = self._token_provider
        token = None if provider is None else await acall_provider(provider)
        return authorization_scope(
            sent_authorization(token, self._http.headers.get("Authorization"))
        )

    def set_etag_lookup(
        self, lookup: Callable[[str, int, str, str | None], Awaitable[str | None]] | None
    ) -> None:
        """Make `update`/`delete` send `await lookup(resource, id, scope, version)` as If-Match.

        The lookup runs once per request, after authentication, with the scope of the token the
        request is actually sent with.
        """
        if lookup is not None and self._if_match not in self._transport.hooks:
            self._transport.add_hook(self._if_match)
        self._if_match.lookup = lookup

    def add_closer(self, close: Callable[[], Awaitable[None]]) -> None:
        """Await `close` when the client closes, before the HTTP client is closed."""
        self._closers.append(close)

    async def manifest(self, *, refresh: bool = False) -> Manifest:
        """The manifest for this caller, loaded once (`refresh=True` reloads)."""
        if self._manifest is None or refresh:
            self._manifest = await aload_manifest(
                self._http,
                self.base_url,
                manifest_url=self._manifest_url,
                allowed_origins=self.allowed_origins,
            )
        return self._manifest

    async def resource(self, name: str, api_version: str | None = None) -> AsyncResourceClient:
        """A handle for `name` (the highest `api_version` unless given)."""
        return AsyncResourceClient(self, _lookup(await self.manifest(), name, api_version))

    async def _send(self, call: _Call) -> httpx2.Response:
        request = _build(self._http, call)
        try:
            response = await self._http.send(request)
        except httpx2.RequestError as exc:
            raise ConnectionFailed(str(exc)) from exc
        return _checked(response)

    async def send_object(self, call: _Call) -> JsonObject:
        """Internal: run a call that returns a JSON object."""
        return _decode_object(await self._send(call))

    async def send_list(self, call: _Call) -> JsonList:
        """Internal: run a call that returns a JSON array."""
        return _decode_list(await self._send(call))

    async def send_none(self, call: _Call) -> None:
        """Internal: run a call whose body is ignored."""
        await self._send(call)

    async def aclose(self) -> None:
        """Await what `add_closer` registered, then close the HTTP client and its transport."""
        closers, self._closers = self._closers[::-1], []
        errors: list[Exception] = []
        for close in (*closers, self._http.aclose):  # every step runs; the first failure is raised
            try:
                await close()
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise errors[0]

    async def __aenter__(self) -> Self:
        """Enter the async context manager."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close on exit."""
        await self.aclose()
