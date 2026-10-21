"""Typed, frozen models of the CRUD manifest (v1) and discovery via the api-catalog.

Members this version does not know are ignored, as the manifest spec requires.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import httpx2

from craftainer_crud_sdk.errors import ConnectionFailed, ManifestError, problem_from_response
from craftainer_crud_sdk.interfaces import EXTENSION_KEY, RequestContext

__all__ = [
    "CATALOG_PATH",
    "PROFILE",
    "Auth",
    "CacheHints",
    "Capabilities",
    "Events",
    "FieldSpec",
    "Manifest",
    "Pagination",
    "Resource",
    "Schemas",
    "aload_manifest",
    "load_manifest",
    "manifest_url_from_catalog",
    "parse_manifest",
]

PROFILE = "https://craftainer.github.io/schemas/crud-manifest/v1"
CATALOG_PATH = "/.well-known/api-catalog"

type _Obj = Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """One filterable and sortable field."""

    name: str
    value_type: str
    filter_ops: tuple[str, ...]
    sortable: bool
    choices: tuple[str, ...] | None
    numeric: bool
    categorical: bool
    read_only: bool
    required: bool


@dataclass(frozen=True, slots=True)
class Pagination:
    """Offset paging parameters and bounds."""

    style: str
    skip_param: str
    limit_param: str
    sort_param: str
    default_limit: int
    max_limit: int
    total: bool


@dataclass(frozen=True, slots=True)
class Events:
    """The SSE stream of a resource."""

    transport: str
    path: str
    resource: str
    resume_param: str
    replay: str
    keepalive_seconds: float
    actions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What the resource supports."""

    bulk_update: bool
    bulk_delete: bool
    clone: bool
    draft_publish: bool
    archive_restore: bool
    revisions: bool
    events: Events | None
    stats: bool
    predict: bool
    conditional_requests: bool
    owner_scoped: bool
    schedulable: bool


@dataclass(frozen=True, slots=True)
class Auth:
    """Authentication scheme and roles."""

    scheme: str
    oidc_discovery_url: str | None
    public_read: bool
    read_roles: tuple[str, ...]
    write_roles: tuple[str, ...]
    delete_roles: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CacheHints:
    """The manifest `cache` hints of a resource (the SDK policy is `cache.CachePolicy`)."""

    etag: bool
    max_age: int
    stats_ttl: int | None
    stale_while_revalidate: int
    list_etag: str | None
    record_etag: str | None
    revalidate_only: bool


@dataclass(frozen=True, slots=True)
class Schemas:
    """JSON Schemas of the record views (kept as plain mappings)."""

    read: Mapping[str, Any]
    create: Mapping[str, Any]
    update: Mapping[str, Any]
    draft: Mapping[str, Any] | None


@dataclass(frozen=True, slots=True)
class Resource:
    """One resource (`name` + `api_version`)."""

    name: str
    title: str
    api_version: str
    base_path: str
    formats: Mapping[str, str]
    id_field: str
    schemas: Schemas
    fields: tuple[FieldSpec, ...]
    pagination: Pagination
    capabilities: Capabilities
    paths: Mapping[str, str]
    bulk_max_matched: int
    auth: Auth
    cache: CacheHints

    def field(self, name: str) -> FieldSpec | None:
        """The filterable field called `name`, if any."""
        return next((f for f in self.fields if f.name == name), None)


@dataclass(frozen=True, slots=True)
class Manifest:
    """The manifest document, as filtered to the caller."""

    manifest_version: str
    service_name: str
    service_version: str
    resources: tuple[Resource, ...]

    def find(self, name: str, api_version: str | None = None) -> Resource | None:
        """The resource, or the highest `api_version` of `name` when none is given."""
        matches = [r for r in self.resources if r.name == name]
        if api_version is not None:
            matches = [r for r in matches if r.api_version == api_version]
        return max(matches, key=lambda r: _version_order(r.api_version), default=None)


_API_VERSION = re.compile(r"v[0-9]+")  # `api_version` of schemas/crud-manifest/v1.json


def _version_order(api_version: str) -> tuple[int, str]:
    """Numeric order of ``vN`` without `int()` (which refuses very long digit strings)."""
    digits = api_version[1:].lstrip("0")
    return len(digits), digits


def _get[T](obj: _Obj, key: str, kind: type[T] | tuple[type, ...], where: str) -> T:
    value = obj.get(key)
    if not isinstance(value, kind) or (isinstance(value, bool) and kind is not bool):
        raise ManifestError(f"{where}.{key}: missing or of the wrong type")
    return value  # type: ignore[return-value]


def _opt[T](obj: _Obj, key: str, kind: type[T], where: str) -> T | None:
    return None if obj.get(key) is None else _get(obj, key, kind, where)


def _strs(obj: _Obj, key: str, where: str) -> tuple[str, ...]:
    items = _get(obj, key, list, where)
    if not all(isinstance(i, str) for i in items):
        raise ManifestError(f"{where}.{key}: expected a list of strings")
    return tuple(items)


def _object(obj: _Obj, key: str, where: str) -> _Obj:
    return _get(obj, key, dict, where)


def _field(obj: _Obj, where: str) -> FieldSpec:
    choices = obj.get("choices")
    return FieldSpec(
        name=_get(obj, "name", str, where),
        value_type=_get(obj, "value_type", str, where),
        filter_ops=_strs(obj, "filter_ops", where),
        sortable=_get(obj, "sortable", bool, where),
        choices=None if choices is None else _strs(obj, "choices", where),
        numeric=_get(obj, "numeric", bool, where),
        categorical=_get(obj, "categorical", bool, where),
        read_only=_get(obj, "read_only", bool, where),
        required=_get(obj, "required", bool, where),
    )


def _events(obj: _Obj, where: str) -> Events:
    return Events(
        transport=_get(obj, "transport", str, where),
        path=_get(obj, "path", str, where),
        resource=_get(obj, "resource", str, where),
        resume_param=_get(obj, "resume_param", str, where),
        replay=_get(obj, "replay", str, where),
        keepalive_seconds=float(_get(obj, "keepalive_seconds", (int, float), where)),
        actions=_strs(obj, "actions", where),
    )


def _capabilities(obj: _Obj, where: str) -> Capabilities:
    events = obj.get("events")
    return Capabilities(
        bulk_update=_get(obj, "bulk_update", bool, where),
        bulk_delete=_get(obj, "bulk_delete", bool, where),
        clone=_get(obj, "clone", bool, where),
        draft_publish=_get(obj, "draft_publish", bool, where),
        archive_restore=_get(obj, "archive_restore", bool, where),
        revisions=_get(obj, "revisions", bool, where),
        events=None if events is None else _events(_object(obj, "events", where), where),
        stats=_get(obj, "stats", bool, where),
        predict=_get(obj, "predict", bool, where),
        conditional_requests=_get(obj, "conditional_requests", bool, where),
        owner_scoped=_get(obj, "owner_scoped", bool, where),
        schedulable=_get(obj, "schedulable", bool, where),
    )


def _api_version(obj: _Obj, where: str) -> str:
    version = _get(obj, "api_version", str, where)
    if _API_VERSION.fullmatch(version) is None:
        raise ManifestError(f"{where}.api_version: {version!r} does not match ^v[0-9]+$")
    return version


def _resource(obj: _Obj, where: str) -> Resource:
    schemas = _object(obj, "schemas", where)
    pagination = _object(obj, "pagination", where)
    auth = _object(obj, "auth", where)
    cache = _object(obj, "cache", where)
    w = where
    return Resource(
        name=_get(obj, "name", str, w),
        title=_get(obj, "title", str, w),
        api_version=_api_version(obj, w),
        base_path=_get(obj, "base_path", str, w),
        formats={k: v for k, v in _object(obj, "formats", w).items() if isinstance(v, str)},
        id_field=_get(obj, "id_field", str, w),
        schemas=Schemas(
            read=_object(schemas, "read", w + ".schemas"),
            create=_object(schemas, "create", w + ".schemas"),
            update=_object(schemas, "update", w + ".schemas"),
            draft=None if schemas.get("draft") is None else _object(schemas, "draft", w),
        ),
        fields=tuple(
            _field(f, f"{w}.fields[{i}]") for i, f in enumerate(_get(obj, "fields", list, w))
        ),
        pagination=Pagination(
            style=_get(pagination, "style", str, w),
            skip_param=_get(pagination, "skip_param", str, w),
            limit_param=_get(pagination, "limit_param", str, w),
            sort_param=_get(pagination, "sort_param", str, w),
            default_limit=_get(pagination, "default_limit", int, w),
            max_limit=_get(pagination, "max_limit", int, w),
            total=_get(pagination, "total", bool, w),
        ),
        capabilities=_capabilities(_object(obj, "capabilities", w), w + ".capabilities"),
        paths={k: v for k, v in _object(obj, "paths", w).items() if isinstance(v, str)},
        bulk_max_matched=_get(_object(obj, "bulk", w), "max_matched", int, w + ".bulk"),
        auth=Auth(
            scheme=_get(auth, "scheme", str, w),
            oidc_discovery_url=_opt(auth, "oidc_discovery_url", str, w),
            public_read=_get(auth, "public_read", bool, w),
            read_roles=_strs(auth, "read_roles", w),
            write_roles=_strs(auth, "write_roles", w),
            delete_roles=_strs(auth, "delete_roles", w),
        ),
        cache=CacheHints(
            etag=_get(cache, "etag", bool, w),
            max_age=_get(cache, "max_age", int, w),
            stats_ttl=_opt(cache, "stats_ttl", int, w),
            stale_while_revalidate=_get(cache, "stale_while_revalidate", int, w),
            list_etag=_opt(cache, "list_etag", str, w),
            record_etag=_opt(cache, "record_etag", str, w),
            revalidate_only=_get(cache, "revalidate_only", bool, w),
        ),
    )


def parse_manifest(document: object) -> Manifest:
    """Parse a manifest document; unknown members are ignored, unknown versions refused."""
    if not isinstance(document, dict):
        raise ManifestError("the manifest is not a JSON object")
    version = _get(document, "manifest_version", str, "manifest")
    if version != "1":
        raise ManifestError(f"unsupported manifest_version {version!r}")
    service = _object(document, "service", "manifest")
    return Manifest(
        manifest_version=version,
        service_name=_get(service, "name", str, "manifest.service"),
        service_version=_get(service, "version", str, "manifest.service"),
        resources=tuple(
            _resource(r, f"resources[{i}]")
            for i, r in enumerate(_get(document, "resources", list, "manifest"))
        ),
    )


def manifest_url_from_catalog(linkset: object, catalog_url: str) -> str:
    """The absolute manifest URL: the first `describedby` link with the manifest profile."""
    entries = linkset.get("linkset") if isinstance(linkset, dict) else None
    for entry in entries if isinstance(entries, list) else []:
        links = entry.get("describedby") if isinstance(entry, dict) else None
        for link in links if isinstance(links, list) else []:
            if isinstance(link, dict) and link.get("profile") == PROFILE:
                href = link.get("href")
                if isinstance(href, str):
                    return urljoin(catalog_url, href)
    raise ManifestError(f"the api-catalog has no describedby link with profile {PROFILE}")


def _request(
    http: httpx2.Client | httpx2.AsyncClient, url: str, *, catalog: bool
) -> httpx2.Request:
    context = RequestContext(
        resource=None, op="manifest", record_id=None, api_version=None, scope="anon"
    )
    # The catalog shares the "manifest" op; no-store keeps it out of the manifest cache entry.
    headers = {"Accept": "application/json"} | ({"Cache-Control": "no-store"} if catalog else {})
    return http.build_request("GET", url, headers=headers, extensions={EXTENSION_KEY: context})


def _checked_json(response: httpx2.Response) -> object:
    if response.status_code >= 400:  # noqa: PLR2004
        raise problem_from_response(response)
    try:
        return response.json()
    except ValueError as exc:
        raise ManifestError(f"{response.url} is not JSON") from exc


def load_manifest(
    http: httpx2.Client, base_url: str, *, manifest_url: str | None = None
) -> Manifest:
    """Fetch and parse the manifest, via api-catalog discovery or a direct `manifest_url`.

    Raises `ProblemError` subclasses for error responses, `ConnectionFailed` when the request
    cannot be sent and `ManifestError` for an unusable document.
    """
    try:
        if manifest_url is None:
            catalog_url = urljoin(base_url, CATALOG_PATH)
            catalog = _checked_json(http.send(_request(http, catalog_url, catalog=True)))
            manifest_url = manifest_url_from_catalog(catalog, catalog_url)
        return parse_manifest(_checked_json(http.send(_request(http, manifest_url, catalog=False))))
    except httpx2.RequestError as exc:
        raise ConnectionFailed(str(exc)) from exc


async def aload_manifest(
    http: httpx2.AsyncClient, base_url: str, *, manifest_url: str | None = None
) -> Manifest:
    """Async twin of `load_manifest`."""
    try:
        if manifest_url is None:
            catalog_url = urljoin(base_url, CATALOG_PATH)
            catalog = _checked_json(await http.send(_request(http, catalog_url, catalog=True)))
            manifest_url = manifest_url_from_catalog(catalog, catalog_url)
        response = await http.send(_request(http, manifest_url, catalog=False))
        return parse_manifest(_checked_json(response))
    except httpx2.RequestError as exc:
        raise ConnectionFailed(str(exc)) from exc
