"""Background revalidation and `BindsTransport` senders go through the auth flow, not the hooks."""

import asyncio
from typing import cast

import httpx2
import pytest

from craftainer_crud_sdk import (
    AsyncCacheLayer,
    AsyncCrudClient,
    AsyncMemoryStore,
    AsyncTransportHook,
    BindsTransport,
    CachePolicy,
    CrudClient,
    MemoryStore,
    SendRaw,
    SyncSendRaw,
    TransportHook,
    build_key,
    cache_scope,
)
from craftainer_crud_sdk.cache.layer import CacheLayer

from ._support import BASE, HEROES_V1, make_transport

POLICY = CachePolicy(max_age=60, stale_while_revalidate=120)
ETAG = '"v1"'


class Clock:
    """A settable clock."""

    def __init__(self) -> None:
        """Start at 1000."""
        self.now = 1000.0

    def __call__(self) -> float:
        """Now."""
        return self.now


class Tokens:
    """A token provider: `current` until asked to refresh, then `fresh`."""

    def __init__(self, current: str | None, fresh: str | None = None) -> None:
        """Set the two tokens."""
        self.current = current
        self.fresh = fresh
        self.refreshes = 0

    def __call__(self, *, refresh: bool = False) -> str | None:
        """The token."""
        if refresh:
            self.refreshes += 1
            self.current = self.fresh
        return self.current


class Sequence:
    """A token provider returning its tokens in turn (the last one repeats)."""

    def __init__(self, *tokens: str) -> None:
        """Set the tokens."""
        self.tokens = list(tokens)

    def __call__(self, *, refresh: bool = False) -> str | None:
        """The next token."""
        return self.tokens.pop(0) if len(self.tokens) > 1 else self.tokens[0]


class Counting:
    """An async hook counting the requests that reach it."""

    def __init__(self) -> None:
        """Start empty."""
        self.requests: list[httpx2.Request] = []

    async def on_request(self, request: httpx2.Request) -> httpx2.Response | None:
        """Count."""
        self.requests.append(request)
        return None

    async def on_response(
        self, request: httpx2.Request, response: httpx2.Response
    ) -> httpx2.Response:
        """Pass through."""
        return response


class Binder:
    """A sync and async hook that keeps the sender it is bound with."""

    send: SyncSendRaw | SendRaw | None = None
    ran = False

    def bind(self, send: SyncSendRaw | SendRaw) -> None:
        """Keep it."""
        self.send = send

    def on_request(self, request: httpx2.Request) -> httpx2.Response | None:
        """Record that a hook ran."""
        self.ran = True
        return None

    def on_response(self, request: httpx2.Request, response: httpx2.Response) -> httpx2.Response:
        """Pass through."""
        return response


class AsyncBinder(Binder):
    """Async twin."""

    async def on_request(  # type: ignore[override]
        self, request: httpx2.Request
    ) -> httpx2.Response | None:
        """Record that a hook ran."""
        self.ran = True
        return None

    async def on_response(  # type: ignore[override]
        self, request: httpx2.Request, response: httpx2.Response
    ) -> httpx2.Response:
        """Pass through."""
        return response


def _ok(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(200, json=[{"id": 1}], headers={"ETag": ETAG})


async def _stale_setup(
    tokens: Tokens | Sequence, handler: object, wire: list[str | None]
) -> tuple[AsyncCrudClient, AsyncCacheLayer, MemoryStore, Clock, Counting]:
    clock = Clock()

    def recording(request: httpx2.Request) -> httpx2.Response:
        # The auth flow edits one request object across a retry: snapshot what went on the wire.
        if request.url.path == HEROES_V1:
            wire.append(request.headers.get("authorization"))
        return handler(request)  # type: ignore[operator, no-any-return]

    store = MemoryStore(clock=clock)
    layer = AsyncCacheLayer(AsyncMemoryStore(store), {"heroes": POLICY}, clock=clock)
    layer.set_events_active("heroes", True)
    counting = Counting()
    hooks: list[AsyncTransportHook] = [layer, counting]
    client = AsyncCrudClient(
        BASE,
        transport=make_transport(recording),
        hooks=hooks,
        token_provider=tokens,
    )
    return client, layer, store, clock, counting


async def _stale_read(client: AsyncCrudClient, layer: AsyncCacheLayer, clock: Clock) -> None:
    heroes = await client.resource("heroes", "v1")
    await heroes.list()
    clock.now += 100  # past max_age, inside the stale-while-revalidate window
    assert await heroes.list() == [{"id": 1}]  # served stale
    await asyncio.gather(*layer._tasks)


@pytest.mark.asyncio
async def test_background_revalidation_refreshes_a_401_and_skips_the_hooks() -> None:
    """The revalidation gets the provider's token, a refresh and one 401 retry; no hooks."""
    tokens = Tokens("old", "new")

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.headers.get("authorization") == "Bearer old" and request.headers.get(
            "if-none-match"
        ):
            return httpx2.Response(401)
        return _ok(request)

    wire: list[str | None] = []
    client, layer, store, clock, counting = await _stale_setup(tokens, handler, wire)
    async with client:
        await _stale_read(client, layer, clock)
    assert wire == [
        "Bearer old",  # the first read
        "Bearer old",  # background: rejected
        "Bearer new",  # background: retried after the refresh
    ]
    assert tokens.refreshes == 1
    assert len(counting.requests) == 3  # manifest + two foreground reads, not the background ones
    # The retried answer belongs to the new token's scope: it must not land under the old key.
    old_key = build_key("heroes", "list", None, cache_scope("old"), "", api_version="v1")
    new_key = build_key("heroes", "list", None, cache_scope("new"), "", api_version="v1")
    assert store.get(new_key) is None
    entry = store.get(old_key)
    assert entry is not None
    assert entry.stored_at == 1000.0  # untouched


@pytest.mark.asyncio
async def test_background_revalidation_does_not_replay_the_original_token() -> None:
    """A token that changed since the original request is not replaced by its stale header."""
    tokens = Sequence("first", "first", "first", "first", "second")  # catalog, manifest, reads, bg
    wire: list[str | None] = []
    client, layer, store, clock, _ = await _stale_setup(tokens, _ok, wire)
    async with client:
        await _stale_read(client, layer, clock)
    assert wire == [
        "Bearer first",
        "Bearer second",  # not the "Bearer first" the original request carried
    ]
    key = build_key("heroes", "list", None, cache_scope("first"), "", api_version="v1")
    entry = store.get(key)
    assert entry is not None
    assert entry.stored_at == 1000.0  # the other token's answer is not stored under this key


@pytest.mark.asyncio
async def test_background_revalidation_still_refreshes_the_entry() -> None:
    """With a stable token the 304 renews the entry in place."""
    tokens = Tokens("tok")
    wire: list[str | None] = []
    client, layer, store, clock, _ = await _stale_setup(tokens, _ok, wire)
    async with client:
        await _stale_read(client, layer, clock)
    key = build_key("heroes", "list", None, cache_scope("tok"), "", api_version="v1")
    entry = store.get(key)
    assert entry is not None
    assert entry.stored_at == 1100.0
    assert wire == [
        "Bearer tok",
        "Bearer tok",
    ]


def test_sync_sender_authenticates_retries_and_skips_the_hooks() -> None:
    """The sender a sync hook is bound with runs the auth flow (one 401 retry), not the hooks."""
    tokens = Tokens("old", "new")
    wire: list[str | None] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        wire.append(request.headers.get("authorization"))
        if request.headers.get("authorization") == "Bearer old":
            return httpx2.Response(401)
        return _ok(request)

    binder = Binder()
    assert isinstance(binder, BindsTransport)
    hooks: list[TransportHook] = [binder]
    client = CrudClient(BASE, transport=make_transport(handler), hooks=hooks, token_provider=tokens)
    request = httpx2.Request("GET", f"{BASE}{HEROES_V1}", headers={"Authorization": "Bearer stale"})
    response = cast("SyncSendRaw", binder.send)(request)
    assert response.status_code == 200
    assert wire[-2:] == ["Bearer old", "Bearer new"]  # not the stale one it was called with
    assert tokens.refreshes == 1
    assert binder.ran is False
    client.close()


def test_sync_sender_without_a_token_keeps_the_request_header() -> None:
    """No token from the provider: the header the request carries is sent, as in the foreground."""
    wire: list[str | None] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        wire.append(request.headers.get("authorization"))
        return _ok(request)

    binder = Binder()
    client = CrudClient(
        BASE, transport=make_transport(handler), hooks=[binder], token_provider=Tokens(None)
    )
    own = httpx2.Request("GET", f"{BASE}{HEROES_V1}", headers={"Authorization": "Basic abc"})
    cast("SyncSendRaw", binder.send)(own)
    assert wire == ["Basic abc"]
    client.close()


def test_sync_sender_closes_the_rejected_response_when_the_refresh_fails() -> None:
    """A provider that raises on refresh must not leave the 401 response open."""
    rejected = httpx2.Response(401)

    def provider(*, refresh: bool = False) -> str | None:
        if refresh:
            raise RuntimeError("no refresh")
        return "old"

    binder = Binder()
    client = CrudClient(
        BASE, transport=make_transport(lambda r: rejected), hooks=[binder], token_provider=provider
    )
    with pytest.raises(RuntimeError, match="no refresh"):
        cast("SyncSendRaw", binder.send)(httpx2.Request("GET", f"{BASE}{HEROES_V1}"))
    assert rejected.is_closed
    client.close()


@pytest.mark.asyncio
async def test_async_sender_authenticates_and_closes_on_a_failed_refresh() -> None:
    """The async sender retries a 401 once; a failing refresh closes the rejected response."""
    rejected = httpx2.Response(401)

    def handler(request: httpx2.Request) -> httpx2.Response:
        return rejected if request.headers.get("authorization") == "Bearer old" else _ok(request)

    binder = AsyncBinder()
    client = AsyncCrudClient(
        BASE,
        transport=make_transport(handler),
        hooks=[binder],
        token_provider=Tokens("old", "new"),
    )
    send = cast("SendRaw", binder.send)
    assert (await send(httpx2.Request("GET", f"{BASE}{HEROES_V1}"))).status_code == 200
    await client.aclose()

    def failing(*, refresh: bool = False) -> str | None:
        if refresh:
            raise RuntimeError("no refresh")
        return "old"

    other = AsyncBinder()
    client = AsyncCrudClient(
        BASE, transport=make_transport(handler), hooks=[other], token_provider=failing
    )
    rejected = httpx2.Response(401)
    with pytest.raises(RuntimeError, match="no refresh"):
        await cast("SendRaw", other.send)(httpx2.Request("GET", f"{BASE}{HEROES_V1}"))
    assert rejected.is_closed
    await client.aclose()


@pytest.mark.asyncio
async def test_async_sender_without_a_token_provider() -> None:
    """Without a provider the async sender sends the request as it is, past the hooks."""
    wire: list[str | None] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        wire.append(request.headers.get("authorization"))
        return _ok(request)

    binder = AsyncBinder()
    client = AsyncCrudClient(BASE, transport=make_transport(handler), hooks=[binder])
    request = httpx2.Request("GET", f"{BASE}{HEROES_V1}", headers={"Authorization": "Basic abc"})
    response = await cast("SendRaw", binder.send)(request)
    assert response.status_code == 200
    assert wire == ["Basic abc"]
    assert binder.ran is False
    await client.aclose()


def test_cache_layer_is_still_a_plain_hook() -> None:
    """The sync layer does not bind (no background work)."""
    assert not isinstance(CacheLayer(MemoryStore(), {}), BindsTransport)
