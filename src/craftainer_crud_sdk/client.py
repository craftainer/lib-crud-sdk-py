"""Sync and async CRUD clients: manifest-driven resource handles over hooked httpx2 transports."""

from __future__ import annotations

import builtins
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self, TypedDict, Unpack, cast
from urllib.parse import urljoin

import httpx2

from craftainer_crud_sdk.auth import BearerAuth
from craftainer_crud_sdk.errors import (
    CapabilityError,
    ConnectionFailed,
    InvalidQueryError,
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
    short-circuiting one (all of them for a network response).
    """

    def __init__(self, inner: httpx2.BaseTransport, hooks: Sequence[TransportHook]) -> None:
        """Wrap `inner`; hooks that implement `BindsTransport` receive `send_raw`."""
        self._inner = inner
        self._hooks = tuple(hooks)
        for hook in self._hooks:
            if isinstance(hook, BindsTransport):
                hook.bind(self.send_raw)

    def send_raw(self, request: httpx2.Request) -> httpx2.Response:
        """Send past all hooks."""
        return self._inner.handle_request(request)

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        """Run the hooks around the inner transport."""
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
        self, inner: httpx2.AsyncBaseTransport, hooks: Sequence[AsyncTransportHook]
    ) -> None:
        """Wrap `inner`; hooks that implement `BindsTransport` receive `send_raw`."""
        self._inner = inner
        self._hooks = tuple(hooks)
        for hook in self._hooks:
            if isinstance(hook, BindsTransport):
                hook.bind(self.send_raw)

    async def send_raw(self, request: httpx2.Request) -> httpx2.Response:
        """Send past all hooks."""
        return await self._inner.handle_async_request(request)

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        """Run the hooks around the inner transport."""
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


def _build(http: httpx2.Client | httpx2.AsyncClient, call: _Call) -> httpx2.Request:
    headers = {"Accept": "application/json", **(call.headers or {})}
    # `build_request` keeps the context dict by reference, so `BearerAuth` can fill the scope.
    return http.build_request(
        call.method,
        call.url,
        json=call.json,
        headers=headers,
        extensions={EXTENSION_KEY: call.context},
    )


def _checked(response: httpx2.Response) -> httpx2.Response:
    if response.status_code >= 400:  # noqa: PLR2004
        raise problem_from_response(response)
    return response


def _decode(response: httpx2.Response) -> object:
    return None if response.status_code == httpx2.codes.NO_CONTENT else response.json()


class _QueryArgs(TypedDict, total=False):
    record_id: int | None
    filters: Mapping[str, object] | None
    sort: str | Sequence[str] | None
    skip: int | None
    limit: int | None
    include_archived: bool | None
    include_unpublished: bool | None


class _Plan:
    """Builds the requests of one resource; shared by the sync and async handles."""

    def __init__(self, base_url: str, resource: Resource) -> None:
        self._base_url = base_url
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
        url = urljoin(self._base_url, path) + (f"?{query}" if query else "")
        context = RequestContext(
            resource=self.resource.name, op=op, record_id=record_id, scope="anon"
        )
        headers = {"If-Match": if_match} if if_match is not None else None
        return _Call(method, url, context, json, headers)

    def _query(self, **kwargs: Unpack[_QueryArgs]) -> str:
        return build_query(
            self.resource.fields, max_limit=self.resource.pagination.max_limit, **kwargs
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
        return urljoin(self._base_url, events.path)

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
        if (record_id is None) == (not filters):
            raise InvalidQueryError("restore takes either an id or filters")
        query = self._query(record_id=record_id) if filters is None else self._bulk_filters(filters)
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
        super().__init__(client.base_url, resource)
        self._client = client

    def list(  # noqa: PLR0913
        self,
        filters: Filters | None = None,
        sort: Sort | None = None,
        *,
        skip: int | None = None,
        limit: int | None = None,
        include_archived: bool | None = None,
        include_unpublished: bool | None = None,
    ) -> JsonList:
        """One page of records."""
        call = self.list_call(
            filters=filters, sort=sort, skip=skip, limit=limit,
            include_archived=include_archived, include_unpublished=include_unpublished,
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
        include_unpublished: bool | None = None,
    ) -> Iterator[JsonObject]:
        """Every record, paging with `skip += limit` until a page shorter than `limit`."""
        size = limit or self.resource.pagination.default_limit
        while True:
            page = self.list(
                filters,
                sort,
                skip=skip,
                limit=size,
                include_archived=include_archived,
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
        """Patch one record; `if_match` is an ETag (`412` when stale)."""
        call = self.update_call(record_id, data, if_match)
        return self._client.send_object(call)

    def update_many(self, filters: Filters, data: Mapping[str, Any]) -> JsonObject:
        """Patch every record matching `filters`; returns `{matched, ids}`."""
        return self._client.send_object(self.update_many_call(filters, data))

    def delete(self, record_id: int, if_match: str | None = None) -> None:
        """Delete one record."""
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
    ) -> None:
        """`base_url` is the server origin (or any URL on it); `manifest_url` skips discovery.

        The client builds its own `httpx2.Client` around `transport` (default: a plain
        `httpx2.HTTPTransport`, which does not read proxy environment variables), wrapped in a
        `HookedTransport` that runs `hooks`.
        """
        self.base_url = base_url
        self._manifest_url = manifest_url
        self._manifest: Manifest | None = None
        self._http = httpx2.Client(
            transport=HookedTransport(transport or httpx2.HTTPTransport(), hooks),
            auth=BearerAuth(token_provider) if token_provider is not None else None,
            timeout=timeout,
            headers=headers,
        )

    def manifest(self, *, refresh: bool = False) -> Manifest:
        """The manifest for this caller, loaded once (`refresh=True` reloads)."""
        if self._manifest is None or refresh:
            self._manifest = load_manifest(
                self._http, self.base_url, manifest_url=self._manifest_url
            )
        return self._manifest

    def resource(self, name: str, api_version: str | None = None) -> ResourceClient:
        """A handle for `name` (the highest `api_version` unless given)."""
        return ResourceClient(self, _lookup(self.manifest(), name, api_version))

    def _send(self, call: _Call) -> object:
        request = _build(self._http, call)
        try:
            response = self._http.send(request)
        except httpx2.RequestError as exc:
            raise ConnectionFailed(str(exc)) from exc
        return _decode(_checked(response))

    def send_object(self, call: _Call) -> JsonObject:
        """Internal: run a call that returns a JSON object."""
        return cast("JsonObject", self._send(call))

    def send_list(self, call: _Call) -> JsonList:
        """Internal: run a call that returns a JSON array."""
        return cast("JsonList", self._send(call))

    def send_none(self, call: _Call) -> None:
        """Internal: run a call whose body is ignored."""
        self._send(call)

    def close(self) -> None:
        """Close the HTTP client and its transport."""
        self._http.close()

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
        super().__init__(client.base_url, resource)
        self._client = client

    async def list(  # noqa: PLR0913
        self,
        filters: Filters | None = None,
        sort: Sort | None = None,
        *,
        skip: int | None = None,
        limit: int | None = None,
        include_archived: bool | None = None,
        include_unpublished: bool | None = None,
    ) -> JsonList:
        """One page of records."""
        call = self.list_call(
            filters=filters, sort=sort, skip=skip, limit=limit,
            include_archived=include_archived, include_unpublished=include_unpublished,
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
        include_unpublished: bool | None = None,
    ) -> AsyncIterator[JsonObject]:
        """Every record, paging with `skip += limit` until a page shorter than `limit`."""
        size = limit or self.resource.pagination.default_limit
        while True:
            page = await self.list(
                filters,
                sort,
                skip=skip,
                limit=size,
                include_archived=include_archived,
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
        """Patch one record; `if_match` is an ETag (`412` when stale)."""
        call = self.update_call(record_id, data, if_match)
        return await self._client.send_object(call)

    async def update_many(self, filters: Filters, data: Mapping[str, Any]) -> JsonObject:
        """Patch every record matching `filters`; returns `{matched, ids}`."""
        return await self._client.send_object(self.update_many_call(filters, data))

    async def delete(self, record_id: int, if_match: str | None = None) -> None:
        """Delete one record."""
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
    ) -> None:
        """As `CrudClient`; the token provider may be synchronous or asynchronous."""
        self.base_url = base_url
        self._manifest_url = manifest_url
        self._manifest: Manifest | None = None
        self._http = httpx2.AsyncClient(
            transport=AsyncHookedTransport(transport or httpx2.AsyncHTTPTransport(), hooks),
            auth=BearerAuth(token_provider) if token_provider is not None else None,
            timeout=timeout,
            headers=headers,
        )

    async def manifest(self, *, refresh: bool = False) -> Manifest:
        """The manifest for this caller, loaded once (`refresh=True` reloads)."""
        if self._manifest is None or refresh:
            self._manifest = await aload_manifest(
                self._http, self.base_url, manifest_url=self._manifest_url
            )
        return self._manifest

    async def resource(self, name: str, api_version: str | None = None) -> AsyncResourceClient:
        """A handle for `name` (the highest `api_version` unless given)."""
        return AsyncResourceClient(self, _lookup(await self.manifest(), name, api_version))

    async def _send(self, call: _Call) -> object:
        request = _build(self._http, call)
        try:
            response = await self._http.send(request)
        except httpx2.RequestError as exc:
            raise ConnectionFailed(str(exc)) from exc
        return _decode(_checked(response))

    async def send_object(self, call: _Call) -> JsonObject:
        """Internal: run a call that returns a JSON object."""
        return cast("JsonObject", await self._send(call))

    async def send_list(self, call: _Call) -> JsonList:
        """Internal: run a call that returns a JSON array."""
        return cast("JsonList", await self._send(call))

    async def send_none(self, call: _Call) -> None:
        """Internal: run a call whose body is ignored."""
        await self._send(call)

    async def aclose(self) -> None:
        """Close the HTTP client and its transport."""
        await self._http.aclose()

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
