# 0002. Cache with our own thin layer, invalidated by SSE

## Status

Accepted

## Context

The SDK should avoid refetching unchanged CRUD responses. The server sends
`Cache-Control: private, no-cache` with an `ETag` (`max_age` is 0), so plain HTTP
caching can only revalidate. Callers who want fresh hits without a round trip need
something that ends the fresh window when the data changes, and the server already
streams record events over SSE.

A general HTTP cache library (hishel) was considered. It cannot clear entries by
resource prefix, cannot override `no-cache` with an opt-in TTL backed by SSE, does not
write a `PATCH` response through to the record entry, does not scope keys per caller,
and has no TypeScript twin, while both SDKs must behave identically
(`lib-crud-sdk-ts` follows the same spec).

## Decision

We will implement a thin cache layer in `craftainer_crud_sdk.cache` as a transport
hook (`TransportHook` / `AsyncTransportHook` from `interfaces.py`) and not depend on a
cache library.

- **Keys** are `v1/{resource}/{kind}/{subject}/{api_version}/{scope}/{canonicalQuery}`.
  Versions of a resource never share an entry (v1 and v2 can differ in shape), but they
  share one event stream. The version comes after kind and subject, so one prefix such as
  `v1/R/record/{id}/` or `v1/R/list/` clears an entry in every version, and scope after
  that clears it for every caller. The resource, operation, record id, API version (`_`
  when `None`) and scope come from the request's `RequestContext`, never from the URL or
  the Authorization header (the cache layer does not read it).
- **The scope is derived from the Authorization header actually on the wire**, in the
  transport just before the hooks run, after the client headers, the auth flow and any
  401 retry have settled: `anon` only when there is no Authorization at all, otherwise
  the first 32 hex of the sha256 of the bearer token (any other scheme hashes the whole
  value). A client configured with a static `Authorization` header and no token provider
  therefore never shares entries with another caller through a shared store. The raw
  credential never enters a key or a log.
- **Only GET 200 JSON** is stored; `Cache-Control: no-store` (response or request),
  `Vary: *` and `Vary` on anything but Authorization/Accept/Accept-Encoding are never
  stored. A storable `200` with no ETag and no fresh window deletes the old entry, so no
  stale validator survives. Stored headers drop transfer headers because the body is stored decoded.
- **Every reuse revalidates** with `If-None-Match` by default; a `304` rebuilds the
  stored response with the `304`'s headers merged in.
- **Fresh hits (`max_age > 0`) and stale-while-revalidate are granted only while the
  resource's SSE stream is connected.** The grant is checked twice: an entry stored
  while the stream was down gets zero freshness, and an entry is not served fresh
  while the stream is down. `stats`/`predict` are bounded by `stats_ttl` instead, since
  the server caches those bodies for the same time and events do not carry aggregate
  changes anyway; that is a fresh window only, stale-while-revalidate of `stats`/`predict`
  needs the stream like every other kind. A policy with `revalidate_only` (the manifest hint, honoured by
  `CachePolicy.from_manifest`) never grants a fresh window or stale-while-revalidate,
  whatever `max_age` says.
- **A response in flight during an invalidation is not stored fresh.** Each resource has
  a generation counter, bumped by every event, reconnect clear and successful write. A
  request captures it, and a response whose resource moved on is stored revalidate-only
  (no fresh window, no SWR); if the invalidation lands while the entry is being stored,
  it is deleted again. The async background revalidation captures it when it is sent.
- **Events invalidate; they never apply snapshots.** Prefixes cleared per action follow
  the spec's table, in every API version; a reconnect clears the resource except
  `filters`, and so does the first handshake of a process (a persistent store may hold
  entries from before it); a changed manifest ETag clears every resource's `filters`.
- **Writes keep the cache honest**: a single `PATCH` stores its response as the record
  entry and clears list/stats/predict and revisions; creates, deletes and bulk writes
  clear the affected prefixes; a `412` evicts the record entry. All in every version.
  One response's prefixes (about two per id for a bulk write) are cleared with a single
  store walk: the stores offer `clear_many(prefixes)`, one `SCAN` (memory: one pass)
  tested against all the prefixes; a custom store without it gets one `clear` per prefix.
  The `SCAN` matches a per-resource glob (`v1/{resource}/...`) under the namespace, so it
  does not walk other resources' keys; a per-resource index set was considered and not
  adopted.
- **Stale-while-revalidate is async only** in 0.1 (a background task through the raw
  transport, one per key). The sync client revalidates.
- **Stores** are a thread-safe LRU/TTL memory store and a Redis store (JSON with a
  base64 body, `SET PX`, `SCAN` + `UNLINK` under a namespace, never `FLUSHDB`; an empty
  namespace is a `ValueError`, since `clear()` would reach every key). A Redis
  outage or a corrupt value is a miss and a logged warning, never a failed request.
  Physical retention is `max(fresh + swr, 3600 s)`.
- **SSE resume is explicit.** A reconnect passes `?subscriber_id=` (the manifest's
  `resume_param`) only when the stream replays persistently, never `Last-Event-ID`
  (the server would read the numeric frame id as a subscriber id). A stream silent for
  three keep-alive intervals is dead (the read timeout) and reconnects with exponential
  backoff (1 s to 30 s, jittered). Events are filtered on the manifest's server-side
  `resource` label.
- **The freshness gate closes however the subscriber ends.** Listeners get
  `on_status(False)` on any exit, including a non-HTTP exception, and the sync thread
  state is reset so a later subscribe restarts the stream. `close()` and the last
  unsubscribe close the live response to unblock the reader, and `close()` joins the
  thread, so the HTTP client can be closed afterwards. Each reader has its own stop
  event (sync) or is detached from the state at once (async), so a subscribe that
  arrives while the previous reader is still winding down starts a new one, and the old
  reader never clears or flips the state of the new one. A listener that joins an
  already connected stream is told `on_status(True)` (sync and async alike).

## Consequences

- We own the correctness of a cache. The behaviour is specified once (the SDK spec) and
  tested in both SDKs against the same scenarios.
- Opt-in freshness costs an open SSE connection per resource (ref-counted, so one per
  resource however many listeners); callers who do not run the stream get pure
  revalidation, which is always correct.
- Events clearing whole prefixes is coarse (any record change clears every cached list
  page, in every version) but cannot serve a wrong page; applying snapshots would be
  finer and is a possible later optimisation.
- `filters` entries are never fresh (events never clear them), so they always revalidate.
- The freshness gate is ref-counted per resource: each wiring counts once while its stream
  is connected, and stopping one never closes the gate for another.
- Policies are built per (resource name, api_version) from each manifest entry; the event
  stream and the gate are per resource name.
- Keys carry no origin. A store shared between servers (or between environments that
  expose the same resource names) must use a per-origin namespace (the Redis `namespace`
  argument); the SDK does not enforce it.
- `auto_if_match` looks the ETag up by `(resource, record_id, scope, api_version)`, since
  the record entries of two versions are separate. The lookup runs in a client-owned
  transport hook after authentication, with the scope of the very token the request is
  sent with, so the token provider is called once per request (and a retry after a 401
  looks up again under the refreshed token).
- `AsyncCacheLayer.aclose` also unbinds the raw send, so a stale hit after close is
  served without a background refresh, and it forgets every in-flight key (a revalidation
  cancelled before it started leaves none behind either).
- Only `POST`, `PATCH`, `PUT` and `DELETE` invalidate; `HEAD` and `OPTIONS` pass through.
- `client.scope()` uses the transport's header logic (the provider's token if it returns a
  non-empty one, else the `Authorization` header configured on the client), and both flows
  call a provider as `provider()` and, after a 401, `provider(refresh=True)`. A success
  response whose body is not JSON raises `ResponseError`.
- `invalidate_on_events` raises `TypeError` when the layer and the subscriber are not both
  sync or both async.
- The cache layer and the subscribers are exercised by a deterministic interleaving harness
  (`tests/unit/concurrency/`) that checks the invariants above after every step.
- Redis outages degrade to uncached requests silently apart from the log line.
- `stats_ttl` freshness can serve data up to that long behind without events; callers
  who need otherwise set `stats_ttl=0` in the policy.
