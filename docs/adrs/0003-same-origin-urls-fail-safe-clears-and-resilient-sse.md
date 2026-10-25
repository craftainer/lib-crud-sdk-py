# 0003. Keep request URLs on one origin, fail safe on cache and stream errors

## Status

Accepted

## Context

The client attaches the bearer token to every request, and several URLs come from the
server's documents rather than from the caller: resource paths and the events path in the
manifest, the manifest link in the api-catalog. `urljoin` resolves an absolute URL or a
protocol-relative `//host/...` to that other host, so a compromised or misconfigured
manifest could have the token sent elsewhere. The client also never follows redirects,
which left a `3xx` to be parsed as if it were a result. Two quiet failures weakened the
cache: a Redis clear that failed left stale entries that fresh hits could still serve, and
an SSE reader that hit a non-HTTP error stopped for good (the TypeScript SDK reconnects).
These follow the triage of the previous round of fixes (ADR 0002 stays as is).

## Decision

- Every join onto `base_url` goes through one helper (`origin.join_same_origin`) that
  rejects a result whose origin differs from `base_url`'s, with `ManifestError`. Origins
  compare as scheme, host and effective port. `allowed_origins` (client option, mirrored
  as `allowedOrigins` in the TypeScript SDK) opts further origins in. The caller's explicit
  `manifest_url` is trusted.
- `3xx` other than `304` raises `ResponseError`.
- The event reader logs any `Exception` (`log.exception`; network errors stay a warning),
  backs off with the existing schedule and reconnects. `BaseException`, including
  `CancelledError` and `KeyboardInterrupt`, propagates.
- A store whose `clear`/`clear_many` fails without raising returns `False` (the Redis
  stores do, keeping the warning). The layer then keeps the affected resource (or all, when
  the prefix names none) out of every freshness grant until a clear of that resource
  succeeds (a reconnect reset) or `clear()` succeeds. The `CacheStore` protocol return type
  widens from `None` to `bool | None`, so existing stores keep working.

## Consequences

- A split-origin deployment must list its other origins; the failure is explicit and
  names the option.
- A Redis outage degrades to revalidation on every read until a reset succeeds, instead of
  possibly serving stale data as fresh.
- Custom stores that swallow clear errors should return `False` to get the same behaviour.
