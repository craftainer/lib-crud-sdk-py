"""Tests for the async client, resource handle and hooked transport."""

import dataclasses
from collections.abc import Callable, Sequence

import httpx2
import pytest

from craftainer_crud_sdk import (
    EXTENSION_KEY,
    AsyncCrudClient,
    AsyncHookedTransport,
    AsyncTokenProvider,
    AsyncTransportHook,
    BindsTransport,
    CapabilityError,
    ConnectionFailed,
    InvalidQueryError,
    NotFound,
    SendRaw,
    ServerError,
    UnknownResourceError,
)

from ._support import BASE, HEROES_V2, MANIFEST_PATH, make_transport


class Recorder:
    """An async hook logging its calls."""

    def __init__(self, name: str, log: list[str], short: httpx2.Response | None = None) -> None:
        """Set up the double."""
        self.name = name
        self.log = log
        self.short = short
        self.send: SendRaw | None = None

    async def on_request(self, request: httpx2.Request) -> httpx2.Response | None:
        """On request."""
        if request.extensions[EXTENSION_KEY]["op"] == "manifest":
            return None
        self.log.append(f"req:{self.name}")
        return self.short

    async def on_response(
        self, request: httpx2.Request, response: httpx2.Response
    ) -> httpx2.Response:
        """On response."""
        if request.extensions[EXTENSION_KEY]["op"] != "manifest":
            self.log.append(f"resp:{self.name}")
        return response

    def bind(self, send: SendRaw) -> None:
        """Bind."""
        self.send = send


def _client(
    handler: Callable[[httpx2.Request], httpx2.Response] | None = None,
    *,
    hooks: Sequence[AsyncTransportHook] = (),
    seen: list[httpx2.Request] | None = None,
    token_provider: AsyncTokenProvider | None = None,
) -> AsyncCrudClient:
    transport = make_transport(
        handler or (lambda r: httpx2.Response(200, json={"id": 1})), seen=seen
    )
    return AsyncCrudClient(
        BASE,
        transport=transport,
        hooks=hooks,
        token_provider=token_provider,
    )


@pytest.mark.asyncio
async def test_every_operation() -> None:
    """Test every operation."""
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        """Handler."""
        if request.method == "DELETE" and b"id=" in request.url.query:
            return httpx2.Response(204)
        if request.url.path.endswith("/revisions"):
            return httpx2.Response(200, json=[{"id": 1}])
        return httpx2.Response(200, json={"matched": 1, "ids": [1]})

    client = _client(handler, seen=seen)
    heroes = await client.resource("heroes")
    assert (await client.manifest()).find("heroes") is not None
    await heroes.get(1)
    assert seen[-1].url.query == b"id=1"
    await heroes.create({"name": "A"})
    assert seen[-1].method == "POST"
    await heroes.update(1, {"name": "B"}, if_match='"e"')
    assert (seen[-1].method, seen[-1].headers["if-match"]) == ("PATCH", '"e"')
    await heroes.update_many({"name": "a"}, {"name": "B"})
    assert seen[-1].url.query == b"name=a"
    await heroes.delete(1)
    assert seen[-1].method == "DELETE"
    await heroes.delete_many({"name": "a"})
    await heroes.restore(2)
    assert seen[-1].url.path == f"{HEROES_V2}/restore"
    await heroes.stats("day", include_archived=True)
    assert seen[-1].url.query == b"bucket=day&include_archived=true"
    await heroes.predict("power_level", 2)
    assert seen[-1].url.query == b"field=power_level&periods=2"
    assert await heroes.revisions(1) == [{"id": 1}]
    assert heroes.events_url() == f"{BASE}{HEROES_V2}/events"
    await heroes.list({"name": "a"}, "name", skip=1, limit=2, include_unpublished=True)
    assert seen[-1].url.query == b"name=a&sort=name&skip=1&limit=2&include_unpublished=true"
    assert seen[-1].extensions[EXTENSION_KEY]["op"] == "list"


@pytest.mark.asyncio
async def test_iter_all() -> None:
    """Test iter all."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        """Handler."""
        skip = int(request.url.params["skip"])
        return httpx2.Response(200, json=[{"id": skip}, {"id": skip + 1}][: 2 if skip < 2 else 1])

    heroes = await _client(handler).resource("heroes")
    ids = [r["id"] async for r in heroes.iter_all(limit=2)]
    assert ids == [0, 1, 2]


@pytest.mark.asyncio
async def test_errors_and_gating() -> None:
    """Test errors and gating."""
    client = _client(lambda r: httpx2.Response(404, json={"title": "gone"}))
    heroes = await client.resource("heroes", "v1")
    with pytest.raises(NotFound):
        await heroes.get(1)
    with pytest.raises(CapabilityError):
        await heroes.stats()
    with pytest.raises(InvalidQueryError):
        await heroes.list({"nope": 1})
    with pytest.raises(UnknownResourceError):
        await client.resource("ghost")
    base = heroes.resource
    no_bulk = dataclasses.replace(
        base, capabilities=dataclasses.replace(base.capabilities, bulk_update=False)
    )
    with pytest.raises(CapabilityError):
        await type(heroes)(client, no_bulk).update_many({"name": "x"}, {})


@pytest.mark.asyncio
async def test_connection_failure() -> None:
    """Test connection failure."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        """Handler."""
        raise httpx2.ConnectError("refused", request=request)

    heroes = await _client(handler).resource("heroes")
    with pytest.raises(ConnectionFailed):
        await heroes.get(1)


@pytest.mark.asyncio
async def test_manifest_failure_and_direct_url() -> None:
    """Test manifest failure and direct url."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        """Handler."""
        return httpx2.Response(500, json={})

    client = AsyncCrudClient(
        BASE,
        transport=httpx2.MockTransport(handler),
        manifest_url=f"{BASE}{MANIFEST_PATH}",
    )
    with pytest.raises(ServerError):
        await client.manifest()


@pytest.mark.asyncio
async def test_hooks_order_short_circuit_bind() -> None:
    """Test hooks order short circuit bind."""
    log: list[str] = []
    cached = httpx2.Response(200, json=[{"id": 9}])
    hooks = (Recorder("a", log), Recorder("b", log, short=cached), Recorder("c", log))
    assert isinstance(hooks[0], BindsTransport)
    heroes = await _client(hooks=hooks).resource("heroes")
    assert await heroes.list() == [{"id": 9}]
    assert log == ["req:a", "req:b", "resp:a"]

    log.clear()
    seen: list[httpx2.Request] = []
    hooks2 = (Recorder("a", log), Recorder("b", log))
    heroes = await _client(hooks=hooks2, seen=seen).resource("heroes")
    await heroes.get(1)
    assert log == ["req:a", "req:b", "resp:b", "resp:a"]
    log.clear()
    send = hooks2[0].send
    assert send is not None
    response = await send(httpx2.Request("GET", f"{BASE}{HEROES_V2}"))
    assert response.status_code == 200
    assert log == []


@pytest.mark.asyncio
async def test_transport_close_and_client_lifecycle() -> None:
    """Test transport close and client lifecycle."""
    closed: list[bool] = []

    class Inner(httpx2.AsyncBaseTransport):
        """Inner."""

        async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
            """Handle async request."""
            return httpx2.Response(200)

        async def aclose(self) -> None:
            """Aclose."""
            closed.append(True)

    class Plain:
        """Plain."""

        async def on_request(self, request: httpx2.Request) -> httpx2.Response | None:
            """On request."""
            return None

        async def on_response(
            self, request: httpx2.Request, response: httpx2.Response
        ) -> httpx2.Response:
            """On response."""
            return response

    transport = AsyncHookedTransport(Inner(), [Plain()])
    assert (await transport.handle_async_request(httpx2.Request("GET", BASE))).status_code == 200
    await transport.aclose()
    assert closed == [True]

    async with AsyncCrudClient(
        BASE, transport=make_transport(lambda r: httpx2.Response(200)), headers={"X-A": "b"}
    ) as client:
        pass
    assert client._http.is_closed
    default = AsyncCrudClient(BASE)
    await default.aclose()
    assert default._http.is_closed


@pytest.mark.asyncio
async def test_clone_draft_publish_filters() -> None:
    """Test clone draft publish filters."""
    seen: list[httpx2.Request] = []
    heroes = await _client(lambda r: httpx2.Response(200, json=[]), seen=seen).resource("heroes")
    await heroes.clone(4)
    assert (seen[-1].url.path, seen[-1].url.query) == (f"{HEROES_V2}/clone", b"id=4")
    await heroes.create_draft({"name": "D"})
    assert seen[-1].url.path == f"{HEROES_V2}/draft"
    await heroes.publish(4)
    assert seen[-1].url.path == f"{HEROES_V2}/publish"
    assert await heroes.filters() == []
    assert seen[-1].url.path == f"{HEROES_V2}/filters"


@pytest.mark.asyncio
async def test_async_token_provider_refresh() -> None:
    """Test async token provider refresh."""
    headers: list[str] = []

    async def provider(*, refresh: bool = False) -> str | None:
        """Provider."""
        return "t2" if refresh else "t1"

    def handler(request: httpx2.Request) -> httpx2.Response:
        """Handler."""
        headers.append(request.headers["authorization"])
        return httpx2.Response(401 if len(headers) == 1 else 200, json=[])

    heroes = await _client(handler, token_provider=provider).resource("heroes")
    assert await heroes.list() == []
    assert headers == ["Bearer t1", "Bearer t2"]


@pytest.mark.asyncio
async def test_aclose_still_closes_http_when_a_closer_raises() -> None:
    """A failing closer does not leak the HTTP client."""
    client = _client()

    async def boom() -> None:
        msg = "boom"
        raise RuntimeError(msg)

    client.add_closer(boom)
    with pytest.raises(RuntimeError, match="boom"):
        await client.aclose()
    assert client._http.is_closed
