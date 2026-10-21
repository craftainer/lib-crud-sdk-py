"""Cache scope follows the Authorization header on the wire; auto_if_match asks for a token once."""

import fakeredis
import httpx2
import pytest

from craftainer_crud_sdk import (
    EXTENSION_KEY,
    AsyncCrudClient,
    AsyncMemoryStore,
    CrudClient,
    MemoryStore,
    aenable_cache,
    cache_scope,
    enable_cache,
    static_token,
)
from craftainer_crud_sdk.auth import authorization_scope
from craftainer_crud_sdk.cache.redis import RedisStore

from ._fake_server import FakeServer
from ._support import BASE


class Spy:
    """A sync and async hook that records the scope and Authorization of every request."""

    def __init__(self) -> None:
        """Create it."""
        self.seen: list[tuple[str, str | None]] = []

    def _note(self, request: httpx2.Request) -> None:
        context = request.extensions.get(EXTENSION_KEY)
        if context is not None:
            self.seen.append((context["scope"], request.headers.get("authorization")))

    def on_request(self, request: httpx2.Request) -> httpx2.Response | None:
        """Record."""
        self._note(request)
        return None

    def on_response(self, request: httpx2.Request, response: httpx2.Response) -> httpx2.Response:
        """Pass through."""
        return response


class ASpy(Spy):
    """Async twin."""

    async def on_request(self, request: httpx2.Request) -> httpx2.Response | None:  # type: ignore[override]
        """Record."""
        self._note(request)
        return None

    async def on_response(  # type: ignore[override]
        self, request: httpx2.Request, response: httpx2.Response
    ) -> httpx2.Response:
        """Pass through."""
        return response


def test_authorization_scope() -> None:
    """Only a missing header is anon; a Bearer header equals the token's scope."""
    assert authorization_scope(None) == "anon"
    assert authorization_scope("Bearer abc") == cache_scope("abc")
    assert authorization_scope("bearer abc") == cache_scope("abc")
    basic = authorization_scope("Basic dTpw")
    assert basic not in {"anon", cache_scope("dTpw")}
    assert basic == cache_scope("Basic dTpw")
    assert "dTpw" not in basic


def test_a_static_authorization_header_selects_the_scope() -> None:
    """No token provider, but an Authorization header: the scope is its hash, not anon."""
    server = FakeServer()
    spy = Spy()
    with CrudClient(
        BASE,
        transport=server.transport(),
        hooks=[spy],
        headers={"Authorization": "Bearer user-a"},
    ) as client:
        client.resource("heroes").list()
        assert client.scope() == cache_scope("user-a")
    assert {scope for scope, _ in spy.seen} == {cache_scope("user-a")}  # manifest requests too


def test_a_non_bearer_authorization_header_is_hashed_not_anon() -> None:
    """A Basic header gets its own scope."""
    server = FakeServer()
    spy = Spy()
    with CrudClient(
        BASE, transport=server.transport(), hooks=[spy], headers={"Authorization": "Basic dTpw"}
    ) as client:
        client.resource("heroes").list()
    assert {scope for scope, _ in spy.seen} == {authorization_scope("Basic dTpw")}


def test_callers_with_different_headers_do_not_share_a_redis_store() -> None:
    """Two users, one shared store: the second never reads the first's entries."""
    redis_client = fakeredis.FakeRedis()
    validators: list[list[str | None]] = []
    for user in ("user-a", "user-b"):
        server = FakeServer()
        with CrudClient(
            BASE, transport=server.transport(), headers={"Authorization": f"Bearer {user}"}
        ) as client:
            enable_cache(client, RedisStore(redis_client), events=False)
            heroes = client.resource("heroes")
            heroes.list()
            heroes.list()
        lists = [r for r in server.requests if r.url.path.endswith("/json") and r.method == "GET"]
        validators.append([r.headers.get("if-none-match") for r in lists])
    assert validators[1][0] is None  # user-b's first list found nothing to revalidate
    assert validators[1][1] is not None
    keys = [k.decode() if isinstance(k, bytes) else k for k in redis_client.keys("*")]
    assert any(cache_scope("user-a") in k for k in keys)
    assert any(cache_scope("user-b") in k for k in keys)
    assert not any("/anon/" in k for k in keys if "/list/" in k)


def test_no_authorization_at_all_is_anon() -> None:
    """No header, no provider: anon."""
    server = FakeServer()
    spy = Spy()
    with CrudClient(BASE, transport=server.transport(), hooks=[spy]) as client:
        client.resource("heroes").list()
        assert client.scope() == "anon"
    assert {scope for scope, _ in spy.seen} == {"anon"}


def test_the_provider_is_called_once_per_request_with_auto_if_match() -> None:
    """The If-Match lookup uses the token the request carries, not a second provider call."""
    calls: list[bool] = []

    def provider(*, refresh: bool = False) -> str | None:
        calls.append(refresh)
        return "tok"

    server = FakeServer()
    with CrudClient(BASE, transport=server.transport(), token_provider=provider) as client:
        enable_cache(client, MemoryStore(), events=False, auto_if_match=True)
        heroes = client.resource("heroes")
        heroes.get(1)
        calls.clear()
        heroes.update(1, {"name": "Grace"})
    assert calls == [False]
    assert server.requests[-1].headers["if-match"] == '"r1-1"'


def test_the_lookup_can_be_removed_and_a_miss_sends_no_if_match() -> None:
    """set_etag_lookup(None) stops the lookup; a miss sends nothing."""
    server = FakeServer()
    with CrudClient(BASE, transport=server.transport()) as client:
        layer = enable_cache(client, MemoryStore(), events=False, auto_if_match=True)
        heroes = client.resource("heroes")
        heroes.update(1, {"name": "x"})  # nothing cached
        assert "if-match" not in server.requests[-1].headers
        heroes.get(1)
        client.set_etag_lookup(None)
        heroes.update(1, {"name": "y"})
        assert "if-match" not in server.requests[-1].headers
        client.set_etag_lookup(layer.cached_etag)  # the hook is installed once
        assert len([h for h in client.hooks if type(h).__name__ == "_IfMatchHook"]) == 1


@pytest.mark.asyncio
async def test_async_scope_and_one_provider_call_per_request() -> None:
    """Async client: header scope, anon, and one provider call per auto_if_match request."""
    calls: list[bool] = []

    async def provider(*, refresh: bool = False) -> str | None:
        calls.append(refresh)
        return "tok"

    server = FakeServer()
    spy = ASpy()
    async with AsyncCrudClient(
        BASE, transport=server.transport(), token_provider=provider, hooks=[spy]
    ) as client:
        await aenable_cache(client, AsyncMemoryStore(), events=False, auto_if_match=True)
        heroes = await client.resource("heroes")
        await heroes.get(1)
        calls.clear()
        await heroes.update(1, {"name": "Grace"})
        assert await client.scope() == cache_scope("tok")
    assert calls == [False, False]  # the update, then the explicit scope() above
    assert [r.headers.get("if-match") for r in server.requests if r.method == "PATCH"] == ['"r1-1"']
    assert {scope for scope, _ in spy.seen} == {cache_scope("tok")}

    spy = ASpy()
    async with AsyncCrudClient(
        BASE, transport=FakeServer().transport(), hooks=[spy], headers={"Authorization": "Bearer h"}
    ) as client:
        await (await client.resource("heroes")).list()
        assert await client.scope() == cache_scope("h")
    assert {scope for scope, _ in spy.seen} == {cache_scope("h")}

    async with AsyncCrudClient(BASE, transport=FakeServer().transport()) as client:
        assert await client.scope() == "anon"


@pytest.mark.asyncio
async def test_async_lookup_can_be_removed() -> None:
    """The async hook is installed once and honours set_etag_lookup(None)."""
    server = FakeServer()
    async with AsyncCrudClient(BASE, transport=server.transport()) as client:
        layer = await aenable_cache(client, AsyncMemoryStore(), events=False, auto_if_match=True)
        heroes = await client.resource("heroes")
        await heroes.update(1, {"name": "x"})  # nothing cached
        assert "if-match" not in server.requests[-1].headers
        await heroes.get(1)
        client.set_etag_lookup(None)
        await heroes.update(1, {"name": "y"})
        assert "if-match" not in server.requests[-1].headers
        client.set_etag_lookup(layer.cached_etag)
        assert len([h for h in client.hooks if type(h).__name__ == "_AsyncIfMatchHook"]) == 1


def test_sync_scope_of_a_token_provider() -> None:
    """The public scope() hashes the provider's token like the request does."""
    with CrudClient(
        BASE, transport=FakeServer().transport(), token_provider=static_token("tok")
    ) as c:
        assert c.scope() == cache_scope("tok")
