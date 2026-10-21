"""Bearer authentication for httpx2: token provider, refresh once on 401, cache scope."""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import AsyncGenerator, Generator
from typing import cast

import httpx2

from craftainer_crud_sdk.interfaces import (
    EXTENSION_KEY,
    AsyncTokenProvider,
    RequestContext,
    TokenProvider,
)

__all__ = ["BearerAuth", "cache_scope", "static_token"]

ANON = "anon"


def cache_scope(token: str | None) -> str:
    """Cache scope of a caller: `"anon"`, or the first 32 hex of sha256 of the whole token.

    Nothing in the token is trusted or decoded (a forged `sub` must not select another
    caller's entries). The raw token never leaves this function.
    """
    if token is None:
        return ANON
    return hashlib.sha256(token.encode()).hexdigest()[:32]


def static_token(token: str | None) -> TokenProvider:
    """A provider that always returns `token` (a refresh changes nothing)."""

    def provider(*, refresh: bool = False) -> str | None:
        return token

    return provider


def _apply(request: httpx2.Request, token: str | None) -> None:
    if token is None:
        request.headers.pop("Authorization", None)
    else:
        request.headers["Authorization"] = f"Bearer {token}"
    context = cast("RequestContext | None", request.extensions.get(EXTENSION_KEY))
    if context is not None:
        context["scope"] = cache_scope(token)


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
        token = provider()
        _apply(request, token)
        response = yield request
        if response.status_code == httpx2.codes.UNAUTHORIZED:
            fresh = provider(refresh=True)
            if fresh is not None and fresh != token:
                _apply(request, fresh)
                yield request

    async def async_auth_flow(
        self, request: httpx2.Request
    ) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        """Async flow: as the sync one, awaiting the provider when it is a coroutine function."""
        token = await self._call()
        _apply(request, token)
        response = yield request
        if response.status_code == httpx2.codes.UNAUTHORIZED:
            fresh = await self._call(refresh=True)
            if fresh is not None and fresh != token:
                _apply(request, fresh)
                yield request

    async def _call(self, *, refresh: bool = False) -> str | None:
        result = self._provider(refresh=refresh)
        if inspect.isawaitable(result):
            return await result
        return result
