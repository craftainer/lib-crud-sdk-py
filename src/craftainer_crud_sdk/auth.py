"""Bearer authentication for httpx2: token provider, refresh once on 401, cache scope."""

from __future__ import annotations

import hashlib
import inspect
import ipaddress
from collections.abc import AsyncGenerator, Generator
from typing import cast

import httpx2

from craftainer_crud_sdk.errors import InsecureTransportError
from craftainer_crud_sdk.interfaces import (
    EXTENSION_KEY,
    AsyncTokenProvider,
    RequestContext,
    TokenProvider,
)

__all__ = [
    "BearerAuth",
    "acall_provider",
    "authorization_scope",
    "cache_scope",
    "call_provider",
    "check_transport",
    "is_insecure_remote",
    "sent_authorization",
    "stamp_scope",
    "static_token",
]

ANON = "anon"


def cache_scope(token: str | None) -> str:
    """Cache scope of a caller: `"anon"`, or the first 32 hex of sha256 of the whole token.

    Nothing in the token is trusted or decoded (a forged `sub` must not select another
    caller's entries). The raw token never leaves this function.
    """
    if token is None:
        return ANON
    return hashlib.sha256(token.encode()).hexdigest()[:32]


_BEARER = "bearer "


def authorization_scope(authorization: str | None) -> str:
    """Cache scope of an ``Authorization`` header value: `"anon"` only when there is none.

    A Bearer value hashes its token (so it equals `cache_scope(token)`); any other scheme hashes
    the whole value. Nothing is decoded or trusted, and the credential never leaves this function.
    """
    if authorization is None:
        return ANON
    if authorization[: len(_BEARER)].lower() == _BEARER:
        return cache_scope(authorization[len(_BEARER) :].strip())
    return cache_scope(authorization)


def stamp_scope(request: httpx2.Request) -> None:
    """Set the request context's scope from the ``Authorization`` header the request carries."""
    context = cast("RequestContext | None", request.extensions.get(EXTENSION_KEY))
    if context is not None:
        context["scope"] = authorization_scope(request.headers.get("Authorization"))


def is_insecure_remote(url: httpx2.URL) -> bool:
    """Whether `url` is plain ``http:`` to a host that is not loopback.

    Loopback is ``localhost``, ``*.localhost``, ``127.0.0.0/8`` and ``::1``, judged on the parsed
    host (so ``http://localhost.evil.example`` is not loopback). ``https:`` and any other scheme
    are never insecure here. A host that is not a literal address in those ranges (``127.1``, an
    IPv4-mapped IPv6 address) counts as remote: the check fails closed.
    """
    if url.scheme != "http":
        return False
    host = url.host.lower().removesuffix(".")  # a trailing dot names the same host
    if host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    if isinstance(address, ipaddress.IPv4Address):
        return address not in ipaddress.ip_network("127.0.0.0/8")
    return address != ipaddress.IPv6Address("::1")


def check_transport(request: httpx2.Request, *, allow_insecure_http: bool) -> None:
    """Raise `InsecureTransportError` before a request carrying credentials goes over plain http.

    Judged on the request as it is about to be sent (the header it really has), so a token set
    by the auth flow, a header configured on the client, or a URL rewritten by a hook is covered.
    Anonymous requests and loopback hosts are never refused.
    """
    if (
        not allow_insecure_http
        and "Authorization" in request.headers
        and is_insecure_remote(request.url)
    ):
        origin = f"{request.url.scheme}://{request.url.netloc.decode()}"
        raise InsecureTransportError(
            f"refusing to send credentials over plain http to {origin}; "
            "use https, or pass allow_insecure_http=True to the client to allow it"
        )


def static_token(token: str | None) -> TokenProvider:
    """A provider that always returns `token` (a refresh changes nothing)."""

    def provider(*, refresh: bool = False) -> str | None:
        return token

    return provider


def sent_authorization(token: str | None, configured: str | None) -> str | None:
    """The ``Authorization`` value a request ends up with: the token's, else the configured one.

    No token (or an empty one) sends nothing of its own, so a header the caller configured on the
    client stays. The auth flow and `CrudClient.scope` both go through here.
    """
    return f"Bearer {token}" if token else configured


def call_provider(provider: TokenProvider, *, refresh: bool = False) -> str | None:
    """Call a `TokenProvider`: ``provider()``, or ``provider(refresh=True)`` after a 401.

    Raises `TypeError` for an asynchronous provider, which a synchronous client cannot await.
    """
    result: object = provider(refresh=True) if refresh else provider()
    if inspect.isawaitable(result):
        if inspect.iscoroutine(result):
            result.close()  # never awaited: don't leak a "never awaited" warning
        raise TypeError("a synchronous client needs a synchronous token provider")
    return cast("str | None", result)


async def acall_provider(
    provider: TokenProvider | AsyncTokenProvider, *, refresh: bool = False
) -> str | None:
    """`call_provider` for the async client: calls it the same way, awaiting an async result."""
    result: object = provider(refresh=True) if refresh else provider()
    if inspect.isawaitable(result):
        return cast("str | None", await result)
    return cast("str | None", result)


def _apply(request: httpx2.Request, token: str | None) -> None:
    # No token (or an empty one) sends nothing of its own: an Authorization header the caller
    # configured stays, and the scope follows whatever header the request ends up with.
    if token:
        request.headers["Authorization"] = f"Bearer {token}"
    stamp_scope(request)


class BearerAuth(httpx2.Auth):
    """Adds `Authorization: Bearer`, refreshes once after a 401, records the cache scope.

    The provider may be synchronous or asynchronous; a synchronous client needs a synchronous
    one. The scope is written into the request's `RequestContext` so hooks see who is asking.
    """

    def __init__(self, provider: TokenProvider | AsyncTokenProvider) -> None:
        """Wrap `provider`."""
        self._provider = provider

    def sync_auth_flow(self, request: httpx2.Request) -> Generator[httpx2.Request, httpx2.Response]:
        """Sync flow: set the token, retry once with a refreshed token on 401."""
        provider = cast("TokenProvider", self._provider)
        token = call_provider(provider)
        _apply(request, token)
        response = yield request
        if response.status_code == httpx2.codes.UNAUTHORIZED:
            fresh = call_provider(provider, refresh=True)
            if fresh and fresh != token:
                _apply(request, fresh)
                yield request

    async def async_auth_flow(
        self, request: httpx2.Request
    ) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        """Async flow: as the sync one, awaiting the provider's result when it is awaitable."""
        token = await acall_provider(self._provider)
        _apply(request, token)
        response = yield request
        if response.status_code == httpx2.codes.UNAUTHORIZED:
            fresh = await acall_provider(self._provider, refresh=True)
            if fresh and fresh != token:
                _apply(request, fresh)
                yield request
