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

- **Keys** are `v1/{resource}/{kind}/{subject}/{scope}/{canonicalQuery}`. Scope comes
  after the subject so one prefix clears an entry for every caller. The resource,
  operation, record id and scope come from the request's `RequestContext`, never from
  the URL or the Authorization header.
- **Only GET 200 JSON** is stored; `Cache-Control: no-store` (response or request),
  `Vary: *` and `Vary` on anything but Authorization/Accept/Accept-Encoding are never
  stored. Stored headers drop transfer headers because the body is stored decoded.
- **Every reuse revalidates** with `If-None-Match` by default; a `304` rebuilds the
  stored response with the `304`'s headers merged in.
- **Fresh hits (`max_age > 0`) and stale-while-revalidate are granted only while the
  resource's SSE stream is connected.** The grant is checked twice: an entry stored
  while the stream was down gets zero freshness, and an entry is not served fresh
  while the stream is down. `stats`/`predict` are bounded by `stats_ttl` instead, since
  the server caches those bodies for the same time and events do not carry aggregate
  changes anyway.
- **Events invalidate; they never apply snapshots.** Prefixes cleared per action follow
  the spec's table; a reconnect clears the resource except `filters`; a changed
  manifest ETag clears every resource's `filters`.
- **Writes keep the cache honest**: a single `PATCH` stores its response as the record
  entry and clears list/stats/predict and revisions; creates, deletes and bulk writes
  clear the affected prefixes; a `412` evicts the record entry.
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

## Consequences

- We own the correctness of a cache. The behaviour is specified once (the SDK spec) and
  tested in both SDKs against the same scenarios.
- Opt-in freshness costs an open SSE connection per resource (ref-counted, so one per
  resource however many listeners); callers who do not run the stream get pure
  revalidation, which is always correct.
- Events clearing whole prefixes is coarse (any record change clears every cached list
  page) but cannot serve a wrong page; applying snapshots would be finer and is a
  possible later optimisation.
- Redis outages degrade to uncached requests silently apart from the log line.
- `stats_ttl` freshness can serve data up to that long behind without events; callers
  who need otherwise set `stats_ttl=0` in the policy.
