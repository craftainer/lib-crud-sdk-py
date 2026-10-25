"""3xx responses are errors; `only_archived` list option."""

import httpx2
import pytest

from craftainer_crud_sdk import (
    AsyncCrudClient,
    CacheLayer,
    CachePolicy,
    CrudClient,
    InvalidQueryError,
    ResponseError,
)
from craftainer_crud_sdk.cache import MemoryStore, build_key
from craftainer_crud_sdk.client import _checked
from craftainer_crud_sdk.query import build_query

from ._support import BASE, load, make_transport

FIELDS = load().resources[0].fields


@pytest.mark.parametrize("code", [300, 301, 302, 303, 307, 308])
def test_a_redirect_is_a_response_error(code: int) -> None:
    """The client never follows redirects, so any 3xx other than 304 is an error."""
    transport = make_transport(
        lambda r: httpx2.Response(code, headers={"Location": "https://evil.example/x"})
    )
    with CrudClient(BASE, transport=transport) as client:
        heroes = client.resource("heroes", "v2")
        with pytest.raises(ResponseError, match=f"{code} redirect to 'https://evil.example/x'"):
            heroes.get(1)
        with pytest.raises(ResponseError):
            heroes.list()


def test_a_redirect_without_location_is_a_response_error() -> None:
    """The message omits the target when there is none."""
    transport = make_transport(lambda r: httpx2.Response(302))
    with CrudClient(BASE, transport=transport) as client, pytest.raises(ResponseError) as info:
        client.resource("heroes", "v2").get(1)
    assert "redirect;" in str(info.value)
    assert info.value.response is not None


@pytest.mark.asyncio
async def test_async_redirect_is_a_response_error() -> None:
    """Async twin."""
    transport = make_transport(lambda r: httpx2.Response(301, headers={"Location": "/x"}))
    async with AsyncCrudClient(BASE, transport=transport) as client:
        heroes = await client.resource("heroes", "v2")
        with pytest.raises(ResponseError, match="301 redirect"):
            await heroes.list()


def test_not_modified_is_not_a_redirect() -> None:
    """304 stays a normal (cache) status: the 3xx check excludes it."""
    response = httpx2.Response(304)
    assert _checked(response) is response


def test_only_archived_is_emitted_after_include_archived_and_only_when_true() -> None:
    """Position and absence on the wire."""
    assert build_query(FIELDS, include_archived=True, only_archived=True) == (
        "include_archived=true&only_archived=true"
    )
    assert build_query(
        FIELDS, skip=0, include_archived=True, only_archived=True, include_unpublished=False
    ) == ("skip=0&include_archived=true&only_archived=true&include_unpublished=false")
    assert build_query(FIELDS, only_archived=True) == "only_archived=true"
    assert build_query(FIELDS, only_archived=False) == ""
    assert build_query(FIELDS, include_archived=False, only_archived=False) == (
        "include_archived=false"
    )


def test_only_archived_with_include_archived_false_is_rejected() -> None:
    """Rejected before any request."""
    with pytest.raises(InvalidQueryError, match="only_archived"):
        build_query(FIELDS, include_archived=False, only_archived=True)
    seen: list[httpx2.Request] = []
    with CrudClient(
        BASE, transport=make_transport(lambda r: httpx2.Response(200, json=[]), seen=seen)
    ) as client:
        heroes = client.resource("heroes", "v2")
        before = len(seen)
        with pytest.raises(InvalidQueryError):
            heroes.list(include_archived=False, only_archived=True)
        with pytest.raises(InvalidQueryError):
            list(heroes.iter_all(include_archived=False, only_archived=True))
        assert len(seen) == before


def test_list_and_iter_all_send_only_archived() -> None:
    """Both entry points pass it through; stats take no such option."""
    seen: list[httpx2.Request] = []
    with CrudClient(
        BASE, transport=make_transport(lambda r: httpx2.Response(200, json=[]), seen=seen)
    ) as client:
        heroes = client.resource("heroes", "v2")
        heroes.list(only_archived=True)
        assert seen[-1].url.query == b"only_archived=true"
        list(heroes.iter_all(limit=5, only_archived=True))
        assert seen[-1].url.query == b"skip=0&limit=5&only_archived=true"
        heroes.list()
        assert seen[-1].url.query == b""
        with pytest.raises(TypeError):
            heroes.stats(only_archived=True)  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_async_list_and_iter_all_send_only_archived() -> None:
    """Async twin, including the rejection."""
    seen: list[httpx2.Request] = []
    async with AsyncCrudClient(
        BASE, transport=make_transport(lambda r: httpx2.Response(200, json=[]), seen=seen)
    ) as client:
        heroes = await client.resource("heroes", "v2")
        await heroes.list(only_archived=True)
        assert seen[-1].url.query == b"only_archived=true"
        _ = [r async for r in heroes.iter_all(limit=5, include_archived=True, only_archived=True)]
        assert seen[-1].url.query == b"skip=0&limit=5&include_archived=true&only_archived=true"
        with pytest.raises(InvalidQueryError):
            await heroes.list(include_archived=False, only_archived=True)


def test_the_cache_key_differs_for_archived_only_lists() -> None:
    """Different wire query, different key: an archived-only list never shares an entry."""
    normal = build_key("heroes", "list", None, "anon", "")
    archived = build_key("heroes", "list", None, "anon", "only_archived=true")
    assert normal != archived
    assert CacheLayer(MemoryStore(), {"heroes": CachePolicy()}) is not None
