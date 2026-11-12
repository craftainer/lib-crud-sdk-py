"""Total deadline and response-size cap for every response the client's transport hands up.

httpx's timeouts are per phase: the read timeout restarts at each received chunk, so a server
that dribbles a byte every few seconds, or streams an endless body, holds the caller for as long
as it likes. These wrappers add what httpx lacks: a wall-clock deadline for the whole exchange and
a cap on the *decoded* body, so a compression bomb counts at its inflated size. Event streams
(``Accept: text/event-stream``) are exempt: they are long-lived by design and httpx2's SSE decoder
bounds each event itself.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass

import httpx2

from craftainer_crud_sdk.errors import DeadlineExceededError, ResponseTooLargeError

__all__ = [
    "DEFAULT_MAX_RESPONSE_BYTES",
    "DEFAULT_TOTAL_TIMEOUT",
    "Bounds",
    "abound",
    "bound",
    "is_event_stream",
]

DEFAULT_TOTAL_TIMEOUT = 30.0
"""Seconds for one request, from sending to the last body byte (6 x httpx's 5 s phase default)."""
DEFAULT_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
"""Decoded body bytes accepted per response (16 MiB)."""

_FRAMING = frozenset({"content-encoding", "content-length", "transfer-encoding"})


@dataclass(frozen=True, slots=True)
class Bounds:
    """Limits for one client; `None` switches a limit off."""

    total_timeout: float | None = DEFAULT_TOTAL_TIMEOUT
    max_response_bytes: int | None = DEFAULT_MAX_RESPONSE_BYTES

    def __post_init__(self) -> None:
        """Reject a limit that is not a positive number."""
        if self.total_timeout is not None and not self.total_timeout > 0:
            raise ValueError("total_timeout must be a positive number of seconds or None")
        if self.max_response_bytes is not None and self.max_response_bytes < 1:
            raise ValueError("max_response_bytes must be a positive integer or None")

    def deadline(self) -> float | None:
        """The monotonic time at which a request started now must be finished."""
        return None if self.total_timeout is None else time.monotonic() + self.total_timeout


def is_event_stream(request: httpx2.Request) -> bool:
    """Whether the request asks for server-sent events (exempt from the bounds)."""
    return "text/event-stream" in request.headers.get("Accept", "").lower()


def _too_large(limit: int) -> ResponseTooLargeError:
    return ResponseTooLargeError(f"response body is larger than max_response_bytes ({limit} bytes)")


def _late(bounds: Bounds) -> DeadlineExceededError:
    return DeadlineExceededError(
        f"request did not finish within total_timeout ({bounds.total_timeout} s)"
    )


def _declared(response: httpx2.Response, bounds: Bounds) -> None:
    """Refuse before reading when a plain (undecoded) ``Content-Length`` already exceeds the cap."""
    limit = bounds.max_response_bytes
    raw = response.headers.get("content-length")
    if limit is not None and raw is not None and raw.isdecimal() and int(raw) > limit:
        raise _too_large(limit)


def _copy(
    response: httpx2.Response, stream: httpx2.SyncByteStream | httpx2.AsyncByteStream
) -> httpx2.Response:
    headers = [(k, v) for k, v in response.headers.multi_items() if k.lower() not in _FRAMING]
    # The client sets `request` on whatever the transport returns, so it is not copied.
    return httpx2.Response(
        response.status_code, headers=headers, stream=stream, extensions=response.extensions
    )


class _Counter:
    def __init__(self, bounds: Bounds, deadline: float | None) -> None:
        self.bounds = bounds
        self.deadline = deadline
        self.seen = 0

    def remaining(self) -> float | None:
        if self.deadline is None:
            return None
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise _late(self.bounds)
        return left

    def count(self, chunk: bytes) -> None:
        self.seen += len(chunk)
        limit = self.bounds.max_response_bytes
        if limit is not None and self.seen > limit:
            raise _too_large(limit)


class _SyncBounded(httpx2.SyncByteStream):
    def __init__(self, source: httpx2.Response, counter: _Counter) -> None:
        self._source = source
        self._counter = counter

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self._source.iter_bytes():
            self._counter.count(chunk)
            self._counter.remaining()
            yield chunk

    def close(self) -> None:
        self._source.close()


class _AsyncBounded(httpx2.AsyncByteStream):
    def __init__(self, source: httpx2.Response, counter: _Counter) -> None:
        self._source = source
        self._counter = counter

    async def __aiter__(self) -> AsyncIterator[bytes]:
        chunks = self._source.aiter_bytes()
        try:
            while True:
                try:
                    async with asyncio.timeout(self._counter.remaining()):
                        chunk = await anext(chunks)
                except StopAsyncIteration:
                    return
                except TimeoutError as exc:
                    raise _late(self._counter.bounds) from exc
                self._counter.count(chunk)
                yield chunk
        finally:
            await chunks.aclose()

    async def aclose(self) -> None:
        await self._source.aclose()


def bound(response: httpx2.Response, bounds: Bounds, deadline: float | None) -> httpx2.Response:
    """`response` with its body bounded (a response already read is only measured)."""
    counter = _Counter(bounds, deadline)
    try:
        _declared(response, bounds)
        counter.remaining()
        try:
            counter.count(response.content)
        except httpx2.ResponseNotRead:
            return _copy(response, _SyncBounded(response, counter))
    except BaseException:
        response.close()
        raise
    return response


async def abound(
    response: httpx2.Response, bounds: Bounds, deadline: float | None
) -> httpx2.Response:
    """Async twin of `bound`."""
    counter = _Counter(bounds, deadline)
    try:
        _declared(response, bounds)
        counter.remaining()
        try:
            counter.count(response.content)
        except httpx2.ResponseNotRead:
            return _copy(response, _AsyncBounded(response, counter))
    except BaseException:
        await response.aclose()
        raise
    return response
