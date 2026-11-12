"""OF1: total deadline, response-size cap and the `iter_all` page bound."""

import asyncio
import gzip
import time
from collections.abc import AsyncIterator, Iterator

import httpx2
import pytest

from craftainer_crud_sdk import (
    AsyncCrudClient,
    AsyncHookedTransport,
    CrudClient,
    DeadlineExceededError,
    HookedTransport,
    PaginationLimitExceededError,
    ResponseTooLargeError,
)
from craftainer_crud_sdk.bounds import Bounds, abound, bound, is_event_stream

from ._support import BASE, make_transport


class _Chunks(httpx2.SyncByteStream):
    def __init__(self, chunks: list[bytes], pause: float = 0.0) -> None:
        self.chunks = chunks
        self.pause = pause

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self.chunks:
            time.sleep(self.pause)
            yield chunk


class _AsyncChunks(httpx2.AsyncByteStream):
    def __init__(self, chunks: list[bytes], pause: float = 0.0) -> None:
        self.chunks = chunks
        self.pause = pause

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            await asyncio.sleep(self.pause)
            yield chunk


def _sync(handler: object, **kwargs: object) -> CrudClient:
    return CrudClient(
        BASE,
        transport=make_transport(handler),  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


def test_defaults_are_documented_values() -> None:
    """30 s total, 16 MiB."""
    bounds = Bounds()
    assert bounds.total_timeout == 30.0
    assert bounds.max_response_bytes == 16 * 1024 * 1024


@pytest.mark.parametrize(("total", "size"), [(0, 1), (-1, 1), (float("nan"), 1), (1, 0)])
def test_bad_limits_are_rejected(total: float, size: int) -> None:
    """A limit must be positive (or None)."""
    with pytest.raises(ValueError, match="positive"):
        CrudClient(BASE, total_timeout=total, max_response_bytes=size)


def test_a_declared_content_length_over_the_cap_is_refused_unread() -> None:
    """The body is never read when Content-Length already exceeds the cap."""

    def handler(_r: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, headers={"Content-Length": "5000"}, stream=_Chunks([b"x"] * 3))

    with (
        _sync(handler, max_response_bytes=100) as client,
        pytest.raises(ResponseTooLargeError, match="max_response_bytes"),
    ):
        client.http.get(f"{BASE}/x")


def test_a_streamed_body_is_counted_without_a_content_length() -> None:
    """A server that streams more than the cap is cut off mid-body."""

    def handler(_r: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, stream=_Chunks([b"x" * 60, b"x" * 60]))

    with _sync(handler, max_response_bytes=100) as client, pytest.raises(ResponseTooLargeError):
        client.http.get(f"{BASE}/x")


def test_a_compression_bomb_counts_at_its_inflated_size() -> None:
    """The cap is on the decoded body."""
    bomb = gzip.compress(b"0" * 1_000_000)
    assert len(bomb) < 5000

    def handler(_r: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, headers={"Content-Encoding": "gzip"}, stream=_Chunks([bomb]))

    with _sync(handler, max_response_bytes=10_000) as client, pytest.raises(ResponseTooLargeError):
        client.http.get(f"{BASE}/x")


def test_a_response_within_the_cap_is_unchanged() -> None:
    """Decoded body, headers and status come through; framing headers are dropped."""
    body = gzip.compress(b'{"a": 1}')

    def handler(_r: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            headers={"Content-Encoding": "gzip", "X-Test": "1", "Content-Type": "application/json"},
            stream=_Chunks([body]),
        )

    with _sync(handler) as client:
        response = client.http.get(f"{BASE}/x")
    assert response.json() == {"a": 1}
    assert response.headers["x-test"] == "1"


def test_an_already_read_response_is_only_measured() -> None:
    """A transport that returns the body in memory (a mock) is still capped."""
    with (
        _sync(lambda _r: httpx2.Response(200, content=b"x" * 200), max_response_bytes=100) as c,
        pytest.raises(ResponseTooLargeError),
    ):
        c.http.get(f"{BASE}/x")
    with _sync(lambda _r: httpx2.Response(200, content=b"x" * 50), max_response_bytes=100) as c:
        assert c.http.get(f"{BASE}/x").content == b"x" * 50


def test_a_slow_body_hits_the_total_deadline() -> None:
    """A server that dribbles bytes inside every per-phase timeout is still stopped."""

    def handler(_r: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, stream=_Chunks([b"x"] * 50, pause=0.02))

    with _sync(handler, total_timeout=0.1) as client:
        started = time.monotonic()
        with pytest.raises(DeadlineExceededError, match="total_timeout"):
            client.http.get(f"{BASE}/x")
    assert time.monotonic() - started < 1.0


def test_the_deadline_is_checked_after_the_headers_too() -> None:
    """Sync cannot interrupt a blocked read, but a late response is refused."""

    def handler(_r: httpx2.Request) -> httpx2.Response:
        time.sleep(0.15)
        return httpx2.Response(200, content=b"{}")

    with _sync(handler, total_timeout=0.05) as client, pytest.raises(DeadlineExceededError):
        client.http.get(f"{BASE}/x")


def test_limits_can_be_switched_off_and_event_streams_are_exempt() -> None:
    """`None` disables a limit; `Accept: text/event-stream` skips both."""

    def handler(_r: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200, headers={"Content-Type": "text/event-stream"}, stream=_Chunks([b"x" * 300])
        )

    with _sync(handler, total_timeout=None, max_response_bytes=None) as client:
        assert len(client.http.get(f"{BASE}/x").content) == 300
    with _sync(handler, total_timeout=0.0001, max_response_bytes=10) as client:
        response = client.http.get(f"{BASE}/e", headers={"Accept": "text/event-stream"})
        assert len(response.content) == 300
    assert is_event_stream(httpx2.Request("GET", BASE, headers={"Accept": "Text/Event-Stream"}))


def test_a_bare_hooked_transport_is_bounded_by_default_and_needs_origins() -> None:
    """Direct construction gets the default bounds; `bounds=None` switches them off."""
    big = httpx2.MockTransport(lambda _r: httpx2.Response(200, content=b"x" * 100))
    with pytest.raises(TypeError, match="trusted_origins"):
        HookedTransport(big, [])  # type: ignore[call-arg]
    transport = HookedTransport(big, [], trusted_origins=None, bounds=Bounds(max_response_bytes=10))
    with httpx2.Client(transport=transport) as client, pytest.raises(ResponseTooLargeError):
        client.get(f"{BASE}/x")
    unbounded = HookedTransport(big, [], trusted_origins=None, bounds=None)
    with httpx2.Client(transport=unbounded) as client:
        assert len(client.get(f"{BASE}/x").content) == 100


def test_bodyless_responses_may_declare_an_entity_length() -> None:
    """HEAD, 204 and 304 name the entity's Content-Length without sending it."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        status = {"/head": 200, "/nocontent": 204, "/notmodified": 304}[request.url.path]
        return httpx2.Response(status, headers={"Content-Length": "99999999999"})

    with _sync(handler, max_response_bytes=100) as client:
        assert client.http.head(f"{BASE}/head").status_code == 200
        assert client.http.get(f"{BASE}/nocontent").status_code == 204
        assert client.http.get(f"{BASE}/notmodified").status_code == 304


def test_an_sse_accept_header_alone_does_not_lift_the_bounds() -> None:
    """The exemption needs an event-stream answer: a huge non-SSE body is still refused."""

    def handler(_r: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200, headers={"Content-Type": "text/html"}, stream=_Chunks([b"x" * 60, b"x" * 60])
        )

    with (
        _sync(handler, max_response_bytes=100) as client,
        pytest.raises(ResponseTooLargeError),
    ):
        client.http.get(f"{BASE}/e", headers={"Accept": "text/event-stream"})


def test_bound_closes_a_refused_response() -> None:
    """The refused response is closed."""
    response = httpx2.Response(200, stream=_Chunks([b"x"]), headers={"Content-Length": "9"})
    with pytest.raises(ResponseTooLargeError):
        bound(httpx2.Request("GET", BASE), response, Bounds(max_response_bytes=5), None)
    assert response.is_closed


@pytest.mark.asyncio
async def test_async_cap_deadline_and_header_wait() -> None:
    """The async transport enforces the same limits, and the wait for headers."""

    async def slow_headers(_r: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(1)
        return httpx2.Response(200, content=b"{}")

    def handler(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if path == "/big":
            return httpx2.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                stream=_AsyncChunks([b"x" * 60, b"x" * 60]),
            )
        if path == "/declared":
            return httpx2.Response(
                200, headers={"content-length": "999"}, stream=_AsyncChunks([b"x"])
            )
        if path == "/mem":
            return httpx2.Response(200, content=b"x" * 500)
        if path == "/bomb":
            return httpx2.Response(
                200,
                headers={"content-encoding": "gzip"},
                stream=_AsyncChunks([gzip.compress(b"0" * 500_000)]),
            )
        if path == "/ok":
            return httpx2.Response(200, stream=_AsyncChunks([b"[1]"]))
        return httpx2.Response(200, stream=_AsyncChunks([b"x"] * 30, pause=0.02))

    async with AsyncCrudClient(
        BASE,
        transport=make_transport(handler),
        max_response_bytes=100,
        total_timeout=0.1,
    ) as client:
        for path in ("/big", "/declared", "/mem", "/bomb"):
            with pytest.raises(ResponseTooLargeError):
                await client.http.get(f"{BASE}{path}")
        assert (await client.http.get(f"{BASE}/ok")).json() == [1]
        with pytest.raises(DeadlineExceededError):
            await client.http.get(f"{BASE}/slow")
        events = await client.http.get(f"{BASE}/big", headers={"Accept": "text/event-stream"})
        assert len(events.content) == 120

    transport = httpx2.MockTransport(slow_headers)
    async with AsyncCrudClient(BASE, transport=transport, total_timeout=0.05) as client:
        with pytest.raises(DeadlineExceededError):
            await client.http.get(f"{BASE}/x")


@pytest.mark.asyncio
async def test_async_limits_off_and_abound_closes_on_refusal() -> None:
    """`None` limits pass; a refused async response is closed."""
    async with AsyncCrudClient(
        BASE,
        transport=make_transport(
            lambda _r: httpx2.Response(200, stream=_AsyncChunks([b"x" * 300]))
        ),
        total_timeout=None,
        max_response_bytes=None,
    ) as client:
        assert len((await client.http.get(f"{BASE}/x")).content) == 300
    response = httpx2.Response(200, stream=_AsyncChunks([b"x"]), headers={"Content-Length": "9"})
    with pytest.raises(ResponseTooLargeError):
        await abound(httpx2.Request("GET", BASE), response, Bounds(max_response_bytes=5), None)
    assert response.is_closed


def _pager(rows: int, ignore_limit: bool = False) -> object:
    def handler(request: httpx2.Request) -> httpx2.Response:
        limit = int(request.url.params["limit"])
        skip = int(request.url.params["skip"])
        count = rows if ignore_limit else min(limit, max(rows - skip, 0))
        return httpx2.Response(200, json=[{"id": skip + i} for i in range(count)])

    return handler


def test_iter_all_stops_at_max_pages() -> None:
    """A server that always returns a full page cannot loop the client forever."""
    with _sync(_pager(10**9)) as client:
        heroes = client.resource("heroes")
        rows = []
        with pytest.raises(PaginationLimitExceededError, match="max_pages"):
            for row in heroes.iter_all(limit=2, max_pages=3):
                rows.append(row)
    assert len(rows) == 6


def test_iter_all_reads_a_collection_that_ends_exactly_at_the_bound() -> None:
    """Short last page, or exactly max_pages full pages followed by an empty one."""
    with _sync(_pager(5)) as client:
        assert len(list(client.resource("heroes").iter_all(limit=2, max_pages=3))) == 5
    with _sync(_pager(6)) as client:
        assert len(list(client.resource("heroes").iter_all(limit=2, max_pages=3))) == 6
        with pytest.raises(PaginationLimitExceededError):
            list(client.resource("heroes").iter_all(limit=2, max_pages=2))
        with pytest.raises(PaginationLimitExceededError):
            list(client.resource("heroes").iter_all(limit=2, max_pages=0))


def test_iter_all_rejects_a_server_that_ignores_limit() -> None:
    """A page larger than `limit` means the server ignores paging."""
    with (
        _sync(_pager(50, ignore_limit=True)) as client,
        pytest.raises(PaginationLimitExceededError, match="ignores the limit"),
    ):
        list(client.resource("heroes").iter_all(limit=2))


@pytest.mark.asyncio
async def test_async_iter_all_bounds() -> None:
    """The async iterator has the same two bounds."""
    async with AsyncCrudClient(BASE, transport=make_transport(_pager(10**9))) as client:  # type: ignore[arg-type]
        heroes = await client.resource("heroes")
        with pytest.raises(PaginationLimitExceededError, match="max_pages"):
            _ = [r async for r in heroes.iter_all(limit=2, max_pages=2)]
    async with AsyncCrudClient(
        BASE,
        transport=make_transport(_pager(50, ignore_limit=True)),  # type: ignore[arg-type]
    ) as client:
        heroes = await client.resource("heroes")
        with pytest.raises(PaginationLimitExceededError, match="ignores"):
            _ = [r async for r in heroes.iter_all(limit=2)]
    async with AsyncCrudClient(BASE, transport=make_transport(_pager(4))) as client:  # type: ignore[arg-type]
        heroes = await client.resource("heroes")
        assert len([r async for r in heroes.iter_all(limit=2)]) == 4
        assert len([r async for r in heroes.iter_all(limit=2, max_pages=2)]) == 4


def test_event_streams_through_the_client_are_not_capped() -> None:
    """`client.http.sse` sends the SSE Accept header, so a long stream is not cut off."""
    frame = b"data: " + b"x" * 40 + b"\n\n"

    def handler(_r: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=_Chunks([frame] * 10, pause=0.02),
        )

    with (
        _sync(handler, max_response_bytes=100, total_timeout=0.05) as client,
        client.http.sse(f"{BASE}/events") as source,
    ):
        assert len(list(source)) == 10


@pytest.mark.asyncio
async def test_a_timeout_error_from_the_transport_is_not_reported_as_our_deadline() -> None:
    """Only the expiry of `total_timeout` becomes `DeadlineExceededError`."""

    async def boom(_r: httpx2.Request) -> httpx2.Response:
        raise TimeoutError("inner")

    async with AsyncCrudClient(BASE, transport=httpx2.MockTransport(boom)) as client:
        with pytest.raises(TimeoutError, match="inner") as info:
            await client.http.get(f"{BASE}/x")
    assert not isinstance(info.value, DeadlineExceededError)


@pytest.mark.asyncio
async def test_a_bare_async_hooked_transport_without_bounds_is_unbounded() -> None:
    """`bounds=None` on the async transport switches both limits off."""
    big = httpx2.MockTransport(lambda _r: httpx2.Response(200, content=b"x" * 100))
    transport = AsyncHookedTransport(big, [], trusted_origins=None, bounds=None)
    async with httpx2.AsyncClient(transport=transport) as client:
        assert len((await client.get(f"{BASE}/x")).content) == 100


def test_a_hook_sees_the_request_on_a_bounded_response() -> None:
    """The bounded copy keeps `request`, which hooks read before the client sets it."""
    request = httpx2.Request("GET", BASE)
    response = bound(request, httpx2.Response(200, stream=_Chunks([b"{}"])), Bounds(), None)
    assert response.request is request
