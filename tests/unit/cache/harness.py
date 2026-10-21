"""A tiny hook runner and fake server for driving the cache hooks without the client."""

from collections.abc import Awaitable, Callable
from typing import Any

import httpx2

from craftainer_crud_sdk.interfaces import (
    EXTENSION_KEY,
    AsyncTransportHook,
    Op,
    RequestContext,
    TransportHook,
)

ETAG = '"v1"'


class Clock:
    """Test helper."""

    def __init__(self) -> None:
        """Create it."""
        self.now = 1000.0

    def __call__(self) -> float:
        """Protocol method."""
        return self.now


def make_request(  # noqa: PLR0913  # a request is described by this many independent facts
    op: Op = "list",
    *,
    method: str = "GET",
    resource: str | None = "heroes",
    record_id: int | None = None,
    query: str = "",
    headers: dict[str, str] | None = None,
    scope: str = "anon",
    context: bool = True,
) -> httpx2.Request:
    """Make request."""
    url = "http://t/api/heroes" + (f"?{query}" if query else "")
    extensions: dict[str, Any] = {}
    if context:
        ctx: RequestContext = {
            "resource": resource,
            "op": op,
            "record_id": record_id,
            "scope": scope,
        }
        extensions[EXTENSION_KEY] = ctx
    return httpx2.Request(method, url, headers=headers, extensions=extensions)


class Server:
    """Answers GETs with a body and ETag (304 on a matching If-None-Match); records calls."""

    def __init__(self) -> None:
        """Create it."""
        self.calls: list[httpx2.Request] = []
        self.etag: str | None = ETAG
        self.body: Any = {"id": 1, "name": "a"}
        self.headers: dict[str, str] = {}
        self.status = 200

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        """Protocol method."""
        self.calls.append(request)
        headers = dict(self.headers)
        if self.etag is not None:
            headers["ETag"] = self.etag
        if (
            request.method == "GET"
            and self.etag
            and request.headers.get("if-none-match") == self.etag
        ):
            return httpx2.Response(304, headers={**headers, "X-New": "1"})
        return httpx2.Response(self.status, json=self.body, headers=headers)


class SyncRunner:
    """Test helper."""

    def __init__(
        self, hook: TransportHook, server: Callable[[httpx2.Request], httpx2.Response]
    ) -> None:
        """Create it."""
        self.hook = hook
        self.transport = httpx2.MockTransport(server)

    def send(self, request: httpx2.Request) -> httpx2.Response:
        """Send."""
        short = self.hook.on_request(request)
        if short is not None:
            return short
        response = self.transport.handle_request(request)
        return self.hook.on_response(request, response)


class AsyncRunner:
    """Test helper."""

    def __init__(
        self, hook: AsyncTransportHook, server: Callable[[httpx2.Request], httpx2.Response]
    ) -> None:
        """Create it."""
        self.hook = hook
        self.transport = httpx2.MockTransport(server)

    async def raw(self, request: httpx2.Request) -> httpx2.Response:
        """Raw."""
        return await self.transport.handle_async_request(request)

    async def send(self, request: httpx2.Request) -> httpx2.Response:
        """Send."""
        short = await self.hook.on_request(request)
        if short is not None:
            return short
        response = await self.raw(request)
        return await self.hook.on_response(request, response)


type RawSend = Callable[[httpx2.Request], Awaitable[httpx2.Response]]
