"""Tests for BearerAuth, cache_scope and static_token."""

import base64
import hashlib
import json

import httpx2
import pytest

from craftainer_crud_sdk import (
    EXTENSION_KEY,
    BearerAuth,
    RequestContext,
    TokenProvider,
    cache_scope,
    static_token,
)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:32]


def test_scope_anonymous() -> None:
    """Test scope anonymous."""
    assert cache_scope(None) == "anon"


def _jwt(claims: object) -> str:
    def enc(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).decode().rstrip("=")

    return f"{enc(b'{}')}.{enc(json.dumps(claims).encode())}.sig"


@pytest.mark.parametrize("token", ["opaque", "a.b.c", "a.!!!.c", _jwt({"sub": "u"}), _jwt({})])
def test_scope_hashes_the_whole_token(token: str) -> None:
    """The scope is 32 hex of sha256 of the whole token and never contains it."""
    scope = cache_scope(token)
    assert scope == _digest(token)
    assert len(scope) == 32
    assert token not in scope


def test_same_sub_different_tokens_get_different_scopes() -> None:
    """A forged token with a victim's `sub` cannot land in the victim's scope."""
    real = _jwt({"sub": "user-1", "exp": 1})
    forged = _jwt({"sub": "user-1", "exp": 2})
    assert cache_scope(real) != cache_scope(forged)


def test_static_token() -> None:
    """Test static token."""
    provider = static_token("t")
    assert provider() == "t"
    assert provider(refresh=True) == "t"


def _client(
    provider: TokenProvider, seen: list[httpx2.Request], statuses: list[int]
) -> httpx2.Client:
    def handler(request: httpx2.Request) -> httpx2.Response:
        """Handler."""
        seen.append(request.__class__("GET", request.url, headers=request.headers))
        return httpx2.Response(statuses.pop(0))

    return httpx2.Client(transport=httpx2.MockTransport(handler), auth=BearerAuth(provider))


def _request(client: httpx2.Client | httpx2.AsyncClient) -> httpx2.Request:
    context = RequestContext(resource="r", op="list", record_id=None, scope="anon")
    return client.build_request("GET", "http://x/", extensions={EXTENSION_KEY: context})


class _Tokens:
    def __init__(self, *tokens: str | None) -> None:
        """Set up the double."""
        self.tokens = list(tokens)
        self.refreshes: list[bool] = []

    def __call__(self, *, refresh: bool = False) -> str | None:
        self.refreshes.append(refresh)
        return self.tokens.pop(0)


def test_sync_sets_header_and_scope() -> None:
    """Test sync sets header and scope."""
    seen: list[httpx2.Request] = []
    with _client(_Tokens("tok"), seen, [200]) as client:
        request = _request(client)
        assert client.send(request).status_code == 200
    assert seen[0].headers["authorization"] == "Bearer tok"
    context: RequestContext = request.extensions[EXTENSION_KEY]
    assert context["scope"] == _digest("tok")


def test_sync_refreshes_once_on_401() -> None:
    """Test sync refreshes once on 401."""
    seen: list[httpx2.Request] = []
    tokens = _Tokens("old", "new")
    with _client(tokens, seen, [401, 200]) as client:
        request = _request(client)
        assert client.send(request).status_code == 200
    assert [r.headers["authorization"] for r in seen] == ["Bearer old", "Bearer new"]
    assert tokens.refreshes == [False, True]
    assert request.extensions[EXTENSION_KEY]["scope"] == _digest("new")


def test_sync_second_401_is_returned() -> None:
    """Test sync second 401 is returned."""
    seen: list[httpx2.Request] = []
    with _client(_Tokens("old", "new", "never"), seen, [401, 401]) as client:
        assert client.send(_request(client)).status_code == 401
    assert len(seen) == 2


@pytest.mark.parametrize("fresh", [None, "same"])
def test_sync_no_retry_without_a_new_token(fresh: str | None) -> None:
    """Test sync no retry without a new token."""
    seen: list[httpx2.Request] = []
    with _client(_Tokens("same", fresh), seen, [401]) as client:
        assert client.send(_request(client)).status_code == 401
    assert len(seen) == 1


def test_sync_anonymous_has_no_header_and_no_context_is_fine() -> None:
    """Test sync anonymous has no header and no context is fine."""
    seen: list[httpx2.Request] = []
    with _client(_Tokens(None), seen, [200]) as client:
        client.get("http://x/")
    assert "authorization" not in seen[0].headers


@pytest.mark.asyncio
async def test_async_with_sync_and_async_providers() -> None:
    """Test async with sync and async providers."""
    seen: list[httpx2.Request] = []
    statuses = [401, 200]

    def handler(request: httpx2.Request) -> httpx2.Response:
        """Handler."""
        seen.append(request.__class__("GET", request.url, headers=request.headers))
        return httpx2.Response(statuses.pop(0))

    async def provider(*, refresh: bool = False) -> str | None:
        """Provider."""
        return "new" if refresh else "old"

    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(handler), auth=BearerAuth(provider)
    ) as client:
        request = _request(client)
        assert (await client.send(request)).status_code == 200
    assert [r.headers["authorization"] for r in seen] == ["Bearer old", "Bearer new"]
    assert request.extensions[EXTENSION_KEY]["scope"] == _digest("new")

    seen.clear()
    statuses[:] = [401]
    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(handler), auth=BearerAuth(static_token("t"))
    ) as client:
        assert (await client.get("http://x/")).status_code == 401
    assert len(seen) == 1
