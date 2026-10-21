# 0001. Cache with our own thin layer, invalidated by SSE

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

- **Keys** are `v1/{resource}/{api_version}/{kind}/{subject}/{scope}/{canonicalQuery}`.
  Versions of a resource never share an entry (v1 and v2 can differ in shape), but they
  share one event stream, so event and write invalidation clears `v1/{resource}/` across
  all versions. A key cannot be cleared by kind across versions, hence the whole-resource
  clear. The resource, operation, record id, API version (`_` when `None`) and scope come
  from the request's `RequestContext`, never from the URL or the Authorization header.
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
  changes anyway. A policy with `revalidate_only` (the manifest hint, honoured by
  `CachePolicy.from_manifest`) never grants a fresh window or stale-while-revalidate,
  whatever `max_age` says.
- **A response in flight during an invalidation is not stored fresh.** Each resource has
  a generation counter, bumped by every event, reconnect clear and successful write. A
  request captures it, and a response whose resource moved on is stored revalidate-only
  (no fresh window, no SWR); if the invalidation lands while the entry is being stored,
  it is deleted again. The async background revalidation captures it when it is sent.
- **Events invalidate; they never apply snapshots.** Every event and every reconnect
  clears the whole resource in all versions; a changed manifest ETag clears the
  `filters` of every `(resource, api_version)` in the old and new manifest.
- **Writes keep the cache honest**: any successful write clears the resource in all
  versions, and a single `PATCH` then stores its response as the record entry; a `412`
  evicts that record's entry in the request's API version.
- **Stale-while-revalidate is async only** in 0.1 (a background task through the raw
  transport, one per key). The sync client revalidates.
- **Stores** are a thread-safe LRU/TTL memory store and a Redis store (JSON with a
  base64 body, `SET PX`, `SCAN` + `UNLINK` under a namespace, never `FLUSHDB`). A Redis
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
  thread, so the HTTP client can be closed afterwards.

## Consequences

- We own the correctness of a cache. The behaviour is specified once (the SDK spec) and
  tested in both SDKs against the same scenarios.
- Opt-in freshness costs an open SSE connection per resource (ref-counted, so one per
  resource however many listeners); callers who do not run the stream get pure
  revalidation, which is always correct.
- Clearing the whole resource is coarse (any change clears every cached list page,
  record, revisions entry and `filters`, in every version) but cannot serve a wrong page;
  per-kind clearing needs the versions known, and applying snapshots would be finer. Both
  are possible later optimisations.
- Redis outages degrade to uncached requests silently apart from the log line.
- `stats_ttl` freshness can serve data up to that long behind without events; callers
  who need otherwise set `stats_ttl=0` in the policy.
