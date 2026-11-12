# 0005. Bind credentials to origins, validate the manifest shape, bound requests

## Status

Proposed

## Context

An OWASP review of the Python SDK (12 Low findings) showed that the same-origin guard of ADR 0003
covers only the URLs the SDK joins itself. The token is attached by the auth flow of an
`httpx2.Client` that `CrudClient.http` exposes, so `client.http.get(url)`, an explicit
`manifest_url` or a hook that rewrites a URL send it wherever the URL points. Manifest-supplied
`base_path`, `paths` and `name` were checked for type and origin only, so `?` or `#` in a path
lost or merged the query the client appends (a bulk delete without its filter), and a `name` with
`/` aliased another resource's cache prefix. The timeout is per phase (httpx documents that the
read timeout restarts at each chunk), there was no size cap, `iter_all` could loop forever on a
server that ignores `limit`, a manifest `keepalive_seconds` of `1e9` or `Infinity` disabled
dead-stream detection, exceptions kept the full response (so `Authorization` and the query) and
cache keys (token hash, filter values) were logged.

## Decision

- A send-time guard in the transport (below the hooks and the auth flow, next to ADR 0004's
  check) refuses any request that carries `Authorization` to an origin outside `base_url` plus
  `allowed_origins` with `OriginNotAllowedError`. The comparison is exact on scheme, host and port
  with default ports filled in, the rule browsers apply to credentials. No exception is made for
  an explicit `manifest_url`: the caller adds its origin to `allowed_origins`. A bare
  `HookedTransport` built without `trusted_origins` keeps the old behaviour (no guard); the
  clients always pass it.
- `name` must match `[A-Za-z0-9][A-Za-z0-9_-]{0,63}`. `base_path` and `paths` values must be
  absolute paths (single leading `/`, RFC 3986 path characters, at most 1024 characters, no `?`,
  `#`, backslash, dot-segments or encoded `.`, `/`, `\`, NUL), or an absolute http(s) URL without
  userinfo, query or fragment under the same path rules (kept so split-origin deployments still
  work; the origin guards judge it). Violations raise `ManifestError` when the manifest loads.
- `keepalive_seconds` is clamped to 1-60 s, default 15 s for anything missing, non-numeric,
  non-finite or non-positive. 15 s is the interval the WHATWG server-sent events specification
  suggests for comment keep-alives; 60 s equals common proxy read timeouts (nginx
  `proxy_read_timeout` defaults to 60 s). The dead-stream timeout stays three intervals, so at
  most 180 s.
- Every response of the client's transport (cache hooks and background revalidation included) is
  bounded by `total_timeout` (default 30 s, six times httpx's 5 s phase default) and
  `max_response_bytes` (default 16 MiB, counted on the decoded body so a compression bomb counts
  inflated; large enough for a page of `max_limit` rows or a stats response, small enough that a
  1024-entry memory cache is not unbounded in practice). `None` disables either. Event streams
  (`Accept: text/event-stream`) are exempt. The async transport also bounds the wait for headers
  with `asyncio.timeout`; the sync transport checks the deadline when the headers arrive and per
  chunk, because a blocked socket read cannot be interrupted, so it overruns by up to one phase
  `timeout`.
- `iter_all(max_pages=1000)` stops with `PaginationLimitExceededError`, also when a page holds
  more rows than `limit`.
- Exceptions keep a redacted copy of the response: headers whose names match authorization,
  cookie, token, secret, api key, csrf/xsrf, session or credential are replaced by `[redacted]`
  (OWASP Logging Cheat Sheet: never log credentials or session identifiers), the request URL
  loses userinfo, query and fragment, the request body is dropped. Server-supplied text in
  messages is cut to 300 characters; the full `detail` stays on the exception. Cache keys are
  logged as `v1/<resource>/<kind>/#<sha256[:12]>`.
- The Redis trust boundary is documented, not enforced: entries stay plain JSON without HMAC.
  An optional caller-provided HMAC key was considered and left out by the maintainers' decision;
  Redis authentication, TLS and ACLs belong to the `redis.Redis` client the caller passes in.
- Choices made without an explicit instruction (fail closed): no exemption of `manifest_url` from
  the credential guard; a page larger than `limit` is an error, not a silent end; protocol-
  relative and dot-segment paths are rejected at load rather than at first use.
- Not changed: the `*.localhost` name check of ADR 0004, the `auto_if_match` miss case and the
  bytes-versus-count bound of `MemoryStore` (reported Low findings left open).

## Consequences

- Breaking, released as a 0.x minor (`feat!`): see "Upgrading to 0.4" in the README. Deployments
  that send a token to a second origin must list it in `allowed_origins`; manifests with unusual
  paths or names are refused; very large or slow responses need explicit limits.
- The credential guard also stops an application from using `client.http` for a third-party URL
  with the SDK's token, which is the intent; such calls need their own client.
- Exceptions no longer carry the original request headers, so code that read the token or the
  query from `error.response.request` must use its own request object.
- `total_timeout` for the sync client is best effort (up to one phase `timeout` over); users who
  need a hard limit use the async client.
- The checks are in the transport, so any extra `httpx2` client built on the SDK's transport has
  them too, while a plain `httpx2.Client` with `BearerAuth` does not.
