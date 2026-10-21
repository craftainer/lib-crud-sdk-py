"""A fake craftainer-crud server for the end-to-end tests: ETag/304, PATCH ETag, 412 and SSE."""

import asyncio
import json
import queue
import time
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import httpx2

from ._support import CATALOG, HEROES_V2, MANIFEST_PATH, load_doc

EVENTS_PATH = f"{HEROES_V2}/events"
SSE_HEADERS = {"content-type": "text/event-stream"}


class Feed(httpx2.SyncByteStream, httpx2.AsyncByteStream):
    """An SSE body that stays open: `push` frames into it, `end` closes it."""

    def __init__(self) -> None:
        """Create an open, empty stream."""
        self._chunks: queue.Queue[bytes | None] = queue.Queue()

    def push(self, chunk: bytes) -> None:
        """Send bytes to the reader."""
        self._chunks.put(chunk)

    def end(self) -> None:
        """End the stream (the reader sees EOF)."""
        self._chunks.put(None)

    def __iter__(self) -> Iterator[bytes]:
        """Block until a chunk or the end arrives."""
        while (chunk := self._chunks.get()) is not None:
            yield chunk

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """Poll for a chunk or the end without blocking the loop."""
        while True:
            try:
                chunk = self._chunks.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.002)
                continue
            if chunk is None:
                return
            yield chunk


def frame(data: object, frame_id: int) -> bytes:
    """One SSE frame (no `event:` name, as the server sends them)."""
    return f"id: {frame_id}\ndata: {json.dumps(data)}\n\n".encode()


class Transport(httpx2.MockTransport):
    """A mock transport whose close ends the open SSE streams, as a real close would."""

    def __init__(self, server: FakeServer) -> None:
        """Serve `server`."""
        super().__init__(server)
        self._server = server

    def close(self) -> None:
        """End the streams, then close."""
        self._server.end_streams()
        super().close()

    async def aclose(self) -> None:
        """End the streams, then close."""
        self._server.end_streams()
        await super().aclose()


class FakeServer:
    """Heroes with strong record ETags, a weak list ETag, If-Match, and an event stream."""

    def __init__(self) -> None:
        """Two heroes, version 1 each."""
        self.rows: dict[int, dict[str, Any]] = {
            1: {"id": 1, "name": "Ada", "power_level": 1},
            2: {"id": 2, "name": "Brin", "power_level": 2},
        }
        self.versions = {1: 1, 2: 1}
        self.list_version = 1
        self.no_store = False
        self.log: list[tuple[str, str, int]] = []
        self.requests: list[httpx2.Request] = []
        self.feeds: list[Feed] = []
        self._frame_id = 0

    def transport(self) -> Transport:
        """A transport for a client."""
        return Transport(self)

    # -- state ------------------------------------------------------------------------------

    def etag(self, record_id: int) -> str:
        """The current strong ETag of a record."""
        return f'"r{record_id}-{self.versions[record_id]}"'

    def list_etag(self) -> str:
        """The current weak ETag of the list."""
        return f'W/"l{self.list_version}"'

    def change(self, record_id: int, **fields: object) -> None:
        """Change a record without telling anyone."""
        self.rows[record_id].update(fields)
        self.versions[record_id] += 1
        self.list_version += 1

    def external_update(self, record_id: int, **fields: object) -> None:
        """Another client changed a record: change it and publish the `update` event."""
        self.change(record_id, **fields)
        self.publish("update", record_id)

    def publish(self, action: str, record_id: int | None, resource: str = "hero") -> None:
        """Push an event frame to every open stream."""
        self._frame_id += 1
        data = {
            "resource": resource,
            "record_id": record_id,
            "action": action,
            "snapshot": self.rows.get(record_id or 0, {}),
            "timestamp": "2026-10-20T00:00:00Z",
        }
        for feed in self.feeds:
            feed.push(frame(data, self._frame_id))

    def end_streams(self) -> None:
        """End every open stream."""
        for feed in self.feeds:
            feed.end()

    # -- views --------------------------------------------------------------------------------

    def api(self, method: str | None = None) -> list[tuple[str, str, int]]:
        """Logged requests to the heroes resource (`(method, query, status)`)."""
        return [e for e in self.log if method is None or e[0] == method]

    def sse_requests(self) -> list[httpx2.Request]:
        """The requests that opened an event stream."""
        return [r for r in self.requests if r.url.path == EVENTS_PATH]

    # -- http ---------------------------------------------------------------------------------

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        """Handle one request."""
        self.requests.append(request)
        path = request.url.path
        if path == "/.well-known/api-catalog":
            return httpx2.Response(200, json=CATALOG)
        if path == MANIFEST_PATH:
            return httpx2.Response(200, json=load_doc(), headers={"ETag": 'W/"m"'})
        if path == EVENTS_PATH:
            feed = Feed()
            feed.push(frame({"subscriber_id": "sub-1"}, 0))
            self.feeds.append(feed)
            return httpx2.Response(200, headers=SSE_HEADERS, stream=feed)
        response = self._heroes(request)
        self.log.append((request.method, request.url.query.decode(), response.status_code))
        return response

    def _heroes(self, request: httpx2.Request) -> httpx2.Response:
        raw = request.url.params.get("id")
        record_id = None if raw is None else int(raw)
        if record_id is not None and record_id not in self.rows:
            return httpx2.Response(404, json={"title": "Not Found", "detail": "no such hero"})
        if request.method == "PATCH" and record_id is not None:
            return self._patch(request, record_id)
        return self._get(request, record_id)

    def _get(self, request: httpx2.Request, record_id: int | None) -> httpx2.Response:
        body: object
        if record_id is None:
            etag, body = self.list_etag(), [self.rows[i] for i in sorted(self.rows)]
        else:
            etag, body = self.etag(record_id), self.rows[record_id]
        headers = {
            "ETag": etag,
            "Cache-Control": "no-store" if self.no_store else "private, no-cache",
        }
        if request.headers.get("if-none-match") == etag:
            return httpx2.Response(304, headers=headers)
        return httpx2.Response(200, json=body, headers=headers)

    def _patch(self, request: httpx2.Request, record_id: int) -> httpx2.Response:
        if request.headers.get("if-match", self.etag(record_id)) != self.etag(record_id):
            return httpx2.Response(
                412,
                json={"title": "Precondition Failed", "detail": "ETag mismatch", "status": 412},
                headers={"content-type": "application/problem+json"},
            )
        self.change(record_id, **json.loads(request.content))
        return httpx2.Response(
            200, json=self.rows[record_id], headers={"ETag": self.etag(record_id)}
        )


def wait_until(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    """Poll `condition` from a test thread until it holds."""
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.005)


async def await_until(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    """Poll `condition` on the loop until it holds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        assert loop.time() < deadline, "condition not reached"
        await asyncio.sleep(0.005)
