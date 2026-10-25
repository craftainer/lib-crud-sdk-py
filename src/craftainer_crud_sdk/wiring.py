"""Opt-in caching for a client: policies from the manifest, the layer as a hook, SSE invalidation.

`enable_cache(client, store)` (and `aenable_cache` for the async client) is the one call that joins
the client core and the cache package:

1. it loads the manifest and builds a `CachePolicy` per (resource name, api_version) from each
   entry's `cache` hints (`overrides` replace a resource's policy in every version);
2. it installs a `CacheLayer`/`AsyncCacheLayer` as a transport hook on the live client;
3. with `auto_if_match=True`, `update`/`delete` without `if_match` send the cached record ETag;
4. with `events=True`, it starts one SSE subscriber per resource name (from its newest version)
   that has an event stream, through the client's authenticated HTTP client, and wires it to the
   layer with `invalidate_on_events`.
   Only while a stream is connected may `max_age`/`stale_while_revalidate` serve without a
   request.

Closing the client stops the subscribers (and, for the async client, cancels the layer's
background revalidations). This is a module of its own, not a client method, because the layer
contract keeps `client` and `events` independent of each other.
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict

from craftainer_crud_sdk.cache import (
    AsyncCacheLayer,
    CacheLayer,
    CachePolicy,
)
from craftainer_crud_sdk.cache.layer import PolicyKey
from craftainer_crud_sdk.client import AsyncCrudClient, CrudClient
from craftainer_crud_sdk.errors import UnknownResourceError
from craftainer_crud_sdk.events import AsyncEventSubscriber, EventSubscriber, invalidate_on_events
from craftainer_crud_sdk.interfaces import AsyncCacheStore, CacheStore
from craftainer_crud_sdk.manifest import Manifest, Resource
from craftainer_crud_sdk.origin import join_same_origin

__all__ = ["aenable_cache", "enable_cache"]


def _selected(manifest: Manifest, names: Sequence[str] | None) -> list[Resource]:
    """The newest version of each requested resource (all of them when `names` is `None`)."""
    wanted = names if names is not None else sorted({r.name for r in manifest.resources})
    found: list[Resource] = []
    for name in wanted:
        resource = manifest.find(name)
        if resource is None:
            raise UnknownResourceError(f"the manifest has no resource {name!r} for this caller")
        found.append(resource)
    return found


def _policies(
    manifest: Manifest,
    selected: Sequence[Resource],
    overrides: Mapping[str, CachePolicy] | None,
) -> dict[PolicyKey, CachePolicy]:
    """A policy per (resource name, api_version) from each manifest entry; an override wins."""
    given = overrides or {}
    names = {r.name for r in selected}
    return {
        (r.name, r.api_version): given.get(r.name)
        or CachePolicy.from_manifest({"cache": asdict(r.cache)})
        for r in manifest.resources
        if r.name in names
    }


def _check_overrides(
    selected: Sequence[Resource], overrides: Mapping[str, CachePolicy] | None
) -> None:
    names = {r.name for r in selected}
    unknown = sorted(set(overrides or ()) - names)
    if unknown:
        raise UnknownResourceError(f"cache overrides for unselected resources: {unknown}")


def enable_cache(  # noqa: PLR0913
    client: CrudClient,
    store: CacheStore,
    *,
    resources: Sequence[str] | None = None,
    overrides: Mapping[str, CachePolicy] | None = None,
    events: bool = True,
    auto_if_match: bool = False,
) -> CacheLayer:
    """Cache `resources` (default: every resource in the manifest) of `client` in `store`.

    `overrides` maps a resource name to a `CachePolicy` that replaces the manifest default.
    `events` starts the per-resource SSE invalidation (resources without an event stream are
    cached by revalidation only). Raises `ValueError` if the client already has a cache.
    """
    if any(isinstance(hook, CacheLayer) for hook in client.hooks):
        raise ValueError("this client already has a cache")
    manifest = client.manifest()
    selected = _selected(manifest, resources)
    _check_overrides(selected, overrides)
    layer = CacheLayer(store, _policies(manifest, selected, overrides))
    client.add_hook(layer)
    if auto_if_match:
        client.set_etag_lookup(layer.cached_etag)
    for resource in selected if events else ():
        stream = resource.capabilities.events
        if stream is None:
            continue
        subscriber = EventSubscriber(
            client.http,
            asdict(stream),
            join_same_origin(client.base_url, stream.path, client.allowed_origins),
        )
        stop = invalidate_on_events(layer, subscriber, resource.name)
        client.add_closer(stop, subscriber.close)
    return layer


async def aenable_cache(  # noqa: PLR0913
    client: AsyncCrudClient,
    store: AsyncCacheStore,
    *,
    resources: Sequence[str] | None = None,
    overrides: Mapping[str, CachePolicy] | None = None,
    events: bool = True,
    auto_if_match: bool = False,
) -> AsyncCacheLayer:
    """Async twin of `enable_cache`; the SSE tasks start on the running loop."""
    if any(isinstance(hook, AsyncCacheLayer) for hook in client.hooks):
        raise ValueError("this client already has a cache")
    manifest = await client.manifest()
    selected = _selected(manifest, resources)
    _check_overrides(selected, overrides)
    layer = AsyncCacheLayer(store, _policies(manifest, selected, overrides))
    client.add_hook(layer)
    if auto_if_match:
        client.set_etag_lookup(layer.cached_etag)
    for resource in selected if events else ():
        stream = resource.capabilities.events
        if stream is None:
            continue
        subscriber = AsyncEventSubscriber(
            client.http,
            asdict(stream),
            join_same_origin(client.base_url, stream.path, client.allowed_origins),
        )
        stop = invalidate_on_events(layer, subscriber, resource.name)
        client.add_closer(_closing(stop, subscriber))
    client.add_closer(layer.aclose)
    return layer


def _closing(
    stop: Callable[[], None], subscriber: AsyncEventSubscriber
) -> Callable[[], Awaitable[None]]:
    async def close() -> None:
        stop()
        await subscriber.aclose()

    return close
