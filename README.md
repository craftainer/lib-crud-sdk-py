# craftainer-crud-sdk

> [!NOTE]
> This repository is vibe coded with [Claude](https://claude.com/product/claude-code).

Python client SDK for craftainer-crud APIs, built on
[`httpx2`](https://pypi.org/project/httpx2/): sync and async clients,
HTTP caching with pluggable stores, and server-sent events. Import name:
`craftainer_crud_sdk`; requires Python 3.14+.

```sh
pip install craftainer-crud-sdk            # in-memory cache only
pip install "craftainer-crud-sdk[redis]"   # adds the Redis cache store
```

It is an instance of
[`template-python-package`](https://github.com/craftainer/template-python-package)
(see [`docs/TEMPLATE.md`](docs/TEMPLATE.md) for the conventions shared by
every instance). The cross-module contracts live in
`src/craftainer_crud_sdk/interfaces.py`.

## Quick start

```python
from craftainer_crud_sdk import CachePolicy, CrudClient, MemoryStore, enable_cache, static_token

with CrudClient("https://api.example.com", token_provider=static_token("<jwt>")) as client:
    # Discovery: GET /.well-known/api-catalog -> manifest, loaded on first use.
    print([r.name for r in client.manifest().resources])
    heroes = client.resource("heroes")

    # A list query, validated against the manifest (filter ops, sortable fields) before sending.
    strong = heroes.list(
        {"power_level__min": 10, "name__icontains": "a"}, sort="-power_level", limit=20
    )
    everyone = list(heroes.iter_all(sort="name"))  # pages with skip += limit

    # Opt in to caching: policies come from the manifest `cache` hints, the layer is installed as
    # a transport hook, and one SSE stream per resource invalidates it on every change.
    layer = enable_cache(
        client,
        MemoryStore(max_entries=1024),
        overrides={"heroes": CachePolicy(max_age=60)},  # fresh hits, only while the stream is up
        auto_if_match=True,  # update/delete send the cached ETag
    )
    heroes.list()  # network
    heroes.list()  # no request: fresh, and the event stream is connected
    heroes.update(1, {"name": "Grace"})  # writes through to the record entry, clears the lists
# leaving the block stops the event streams
```

The async client is the same with `await`: `AsyncCrudClient`, `AsyncMemoryStore` and
`await aenable_cache(client, store, ...)`; closing it (`async with` / `await client.aclose()`)
cancels the event tasks and the layer's background revalidations.

`enable_cache(client, store, *, resources=None, overrides=None, events=True,
auto_if_match=False)` caches every resource in the manifest unless `resources` names some;
`overrides` maps a resource name to a `CachePolicy` that replaces its manifest default;
`events=False` skips the SSE streams (every read then revalidates); `auto_if_match=True` makes
`update`/`delete` without `if_match` send the cached record ETag, so a stale write is a
`PreconditionFailed` (`412`) and evicts the record. It is a function, not a client method,
because the import contract keeps `client` and `events` independent of each other. It returns the
layer, whose `clear`, `cached_etag` and `events_active` stay available.

## Requests, origins and errors

- **Same origin by default.** Every URL the client joins onto `base_url` (resource paths, the
  events stream, the api-catalog `describedby` link) must be on `base_url`'s origin (scheme,
  host, port). An absolute URL or a protocol-relative `//host/...` on another origin raises
  `ManifestError` before anything is sent, so a bad manifest cannot make the client send its
  bearer token to another host. For a split-origin deployment, opt in per origin:
  `CrudClient(base_url, allowed_origins=["https://events.example.com"])` (also
  `AsyncCrudClient`; exposed as `client.allowed_origins`). The explicit `manifest_url` you pass
  yourself is trusted as given.
- **No redirects.** The client never follows redirects, so a `3xx` other than `304` raises
  `ResponseError` (the message names the `Location`, `response` holds the response).
- **Archived rows.** `list` and `iter_all` take `include_archived` and `only_archived=False`.
  `only_archived=True` returns only archived rows (it implies `include_archived`), is sent as
  `only_archived=true` right after `include_archived` and only when true, and with
  `include_archived=False` raises `InvalidQueryError` before any request. It is a list option
  only (not `stats`, writes or `get`); the wire query is part of the cache key, so archived-only
  lists never share an entry with normal ones.

## Caching

Caching is opt-in per resource and is a transport hook, so it works the same for the
sync and async clients. A `CachePolicy` is defaulted from the manifest `cache` hints
(`CachePolicy.from_manifest(resource_dict)`; `enable_cache` does this for you) and any field can
be overridden. (`Resource.cache` is the raw `CacheHints` of the manifest, not a policy.)

```python
from craftainer_crud_sdk import CacheLayer, CachePolicy, MemoryStore

# Hand wiring, when `enable_cache` is not enough (`heroes` is the manifest resource dict):
layer = CacheLayer(MemoryStore(max_entries=1024), {"heroes": CachePolicy.from_manifest(heroes)})
```

- By default every reuse is revalidated with `If-None-Match`; a `304` serves the stored
  body with merged headers. Only `GET` `200` JSON is stored, never `no-store`.
- `max_age > 0` (fresh hits) and `stale_while_revalidate` (async only) take effect only
  while the resource's SSE stream is connected, because events are what end freshness
  early (also for `stats`/`predict`, whose `stale_while_revalidate` needs the stream; their
  `stats_ttl` freshness does not). Wire a stream with `invalidate_on_events(layer, subscriber, "heroes")`
  (`EventSubscriber` / `AsyncEventSubscriber`).
  `stats` and `predict` use `stats_ttl` and need no stream.
- Events clear cached prefixes; the first handshake of a stream and every reconnect clear
  the resource. A successful write
  updates or clears what it changed (`PATCH` writes its response through as the record
  entry), and a write the server refuses with `412`, `404` or `410` evicts the record's cached
  reads (its entry and its revisions), as a read answered `404`/`410` does.
- Entries are scoped per caller: the key carries `anon` or a hash of the whole bearer token
  (never the unverified JWT `sub`), plus the resource's `api_version`, so two callers never
  share an entry and two API versions of one resource never do either. A token refresh
  therefore starts a fresh set of entries.
- A stale-while-revalidate background request goes through the same `BearerAuth` as any other
  call (a `401` refreshes the token and retries once) and does not re-enter the cache hooks.
- Stores: `MemoryStore` (LRU plus TTL) and, with the `redis` extra,
  `craftainer_crud_sdk.cache.redis.RedisStore` / `AsyncRedisStore`. A Redis outage is a
  cache miss and a logged warning. Cache keys carry no server identity: a store shared
  between servers or environments must use a per-origin namespace (for `RedisStore`, its
  `namespace` argument, which must not be empty).
- `Set-Cookie` is never stored or replayed.
- A cache clear that fails (a Redis error: `RedisStore.clear` / `clear_many` return `False`, and
  log the warning) marks the affected resource not fresh: no fresh hit, no stale-while-revalidate
  and no `stats_ttl` window until the next successful reset of that resource (the clear after
  a stream reconnect, or `layer.clear()` for everything), so every read revalidates. A custom
  store signals the same by returning `False` from `clear` (and an optional `clear_many`).
- Event streams reconnect with backoff after any `Exception` in the reader, not only network
  errors: the failure is logged with its traceback and the freshness gate closes meanwhile.
  `KeyboardInterrupt`, `CancelledError` and other `BaseException`s are never swallowed.
- `filters` always revalidate. The freshness gate is counted per resource, so several
  streams of one resource keep it open until the last is gone. `enable_cache` builds a
  policy per resource name and `api_version` from each manifest entry.

The design and its reasons are in
[`docs/adrs/0002-own-thin-cache-layer-with-sse-invalidation.md`](docs/adrs/0002-own-thin-cache-layer-with-sse-invalidation.md).

## Publishing to PyPI

Releases use PyPI [trusted publishing](https://docs.pypi.org/trusted-publishers/)
(GitHub OIDC): no API token is stored anywhere. `make publish-packages` skips
with a warning until you opt in, per repository:

1. Register a "pending publisher" on the index before the first release
   (PyPI: Your account, Publishing, "Add a new pending publisher"; TestPyPI
   has its own, separate form on test.pypi.org). Enter the project name from
   `pyproject.toml`, the GitHub owner and repository, the workflow name
   `release.yml`, and leave the environment empty (or use the one
   `release.yml` runs under, if you add one).
2. Set the repository variable `PYPI_PUBLISH` (Settings, Secrets and
   variables, Actions, Variables):
   - unset: `make publish-packages` warns that it skipped and the release only
     attaches the sdist, wheel and SBOM to the GitHub release;
   - `testpypi`: publish to TestPyPI;
   - `pypi`: publish to PyPI.
3. Do the first release with `testpypi` as a dry run (a pre-release channel
   is enough), confirm that the release workflow's `verify-published` job (its last
   job; it runs `make verify-published` after the GitHub release exists, so a
   failure does not undo the release) installs the published version back
   from TestPyPI. The target needs the release's `dist/` and
   `RELEASE_VERSION`, so run it only in that workflow, not locally. Then
   register the PyPI pending publisher and switch the variable to `pypi`.

The version lives only in the git tag `release.yml` computes; `pyproject.toml`
stays at `0.0.0` and `make build` stamps `RELEASE_VERSION` into the build
without committing it.

## License

Licensed under the [GNU General Public License v3.0](LICENSE).
