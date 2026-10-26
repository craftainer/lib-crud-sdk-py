# 0004. Refuse bearer tokens over plain http except to loopback

## Status

Accepted

## Context

The client attaches the bearer token to every request. Over plain `http:` the token is
readable on the wire, and nothing stopped a `base_url` (or an `allowed_origins` entry, or a
URL a hook rewrote) from naming an `http:` host. ADR 0003 keeps requests on one origin but does
not look at the scheme. The TypeScript SDK refuses this since its `feat!` change (#15); the
Python SDK should behave the same. Local development against `http://localhost` must keep
working.

## Decision

- A request that carries an `Authorization` header and whose URL is `http:` to a host that is
  not loopback raises `InsecureTransportError` (a `ClientError`) before it is sent. Loopback is
  `localhost`, `*.localhost`, `127.0.0.0/8` and `::1`, judged on the parsed host. Anything else
  (including `127.1` and IPv4-mapped IPv6 forms) counts as remote, so the check fails closed.
  Anonymous requests are never refused.
- The check sits in `HookedTransport.send_raw` and its async twin, the single point below the
  hooks and the auth flow, so it sees the request as it is sent: the token set by `BearerAuth`
  (also on the refresh-and-retry after a `401`), a header configured on the client, a URL a
  hook rewrote, hooks' `send_authenticated` background requests (SWR revalidation) and the SSE
  streams, which run on `client.http`. The event reader treats the refusal like any stream
  failure: it logs it and retries with backoff, and no token is ever sent.
- `allow_insecure_http=True` on `CrudClient` and `AsyncCrudClient` lifts the check, for a
  trusted network only. The package has no CLI, so there is no flag.
- Not covered: `BearerAuth` used on a caller's own `httpx2` client without the SDK's transport.
- This is a breaking change (`feat!`), released as `craftainer-crud-sdk` 0.3.0.

## Consequences

- Deployments that used a token over plain http to a non-loopback host fail on upgrade with an
  error that names the origin and the option; they either move to https or opt in.
- Tests and fixtures that send a token must use https or loopback hosts.
- Parity with the TypeScript SDK: same loopback set, same error name, same opt-in (spelled
  `allow_insecure_http` here). Unlike the TypeScript SDK, whose event hub takes a caller-supplied
  `fetch`, the Python event streams share the client's authenticated transport and are covered.
