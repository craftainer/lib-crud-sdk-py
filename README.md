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
    strong = heroes.list({"power_level__min": 10, "name__icontains": "a"}, sort="-power_level", limit=20)
    everyone = list(heroes.iter_all(sort="name"))      # pages with skip += limit

    # Opt in to caching: policies come from the manifest `cache` hints, the layer is installed as
    # a transport hook, and one SSE stream per resource invalidates it on every change.
    layer = enable_cache(
        client,
        MemoryStore(max_entries=1024),
        overrides={"heroes": CachePolicy(max_age=60)},  # fresh hits, only while the stream is up
        auto_if_match=True,                             # update/delete send the cached ETag
    )
    heroes.list()    # network
    heroes.list()    # no request: fresh, and the event stream is connected
    heroes.update(1, {"name": "Grace"})   # writes through to the record entry, clears the lists
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
  early. Wire a stream with `invalidate_on_events(layer, subscriber, "heroes")`
  (`EventSubscriber` / `AsyncEventSubscriber`).
  `stats` and `predict` use `stats_ttl` and need no stream.
- Events clear cached prefixes; a reconnect clears the resource. A successful write
  updates or clears what it changed (`PATCH` writes its response through as the record
  entry), and a `412` evicts the record.
- Stores: `MemoryStore` (LRU plus TTL) and, with the `redis` extra,
  `craftainer_crud_sdk.cache.redis.RedisStore` / `AsyncRedisStore`. A Redis outage is a
  cache miss and a logged warning.

The design and its reasons are in
[`docs/adrs/0002-own-thin-cache-layer-with-sse-invalidation.md`](docs/adrs/0002-own-thin-cache-layer-with-sse-invalidation.md).

## Publishing to PyPI

Releases use PyPI [trusted publishing](https://docs.pypi.org/trusted-publishers/)
(GitHub OIDC): no API token is stored anywhere. `make publish` is a no-op
until you opt in, per repository:

1. Register a "pending publisher" on the index before the first release
   (PyPI: Your account, Publishing, "Add a new pending publisher"; TestPyPI
   has its own, separate form on test.pypi.org). Enter the project name from
   `pyproject.toml`, the GitHub owner and repository, the workflow name
   `release.yml`, and leave the environment empty (or use the one
   `release.yml` runs under, if you add one).
2. Set the repository variable `PYPI_PUBLISH` (Settings, Secrets and
   variables, Actions, Variables):
   - unset: `make publish` prints a skip message and the release only
     attaches the sdist, wheel and SBOM to the GitHub release;
   - `testpypi`: publish to TestPyPI;
   - `pypi`: publish to PyPI.
3. Do the first release with `testpypi` as a dry run (a pre-release channel
   is enough), confirm that `make release-check` installs the published
   version back from TestPyPI, then register the PyPI pending publisher and
   switch the variable to `pypi`.

The version lives only in the git tag `release.yml` computes; `pyproject.toml`
stays at `0.0.0` and `make build` stamps `RELEASE_VERSION` into the build
without committing it.

## License

Licensed under the [GNU General Public License v3.0](LICENSE).
