"""OF1: the bearer token goes only to `base_url`'s origin and `allowed_origins`."""

import httpx2
import pytest

from craftainer_crud_sdk import (
    AsyncCrudClient,
    ClientError,
    CrudClient,
    OriginNotAllowedError,
    static_token,
)
from craftainer_crud_sdk.auth import check_origin
from craftainer_crud_sdk.origin import origin_of, trusted_origins

from ._support import BASE, make_transport

EVIL = "https://evil.example"


def _client(seen: list[httpx2.Request], **kwargs: object) -> CrudClient:
    return CrudClient(
        BASE,
        token_provider=static_token("tok"),
        transport=make_transport(lambda _r: httpx2.Response(200, json=[]), seen=seen),
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    "url",
    [
        f"{EVIL}/x",
        "https://crud.test:8443/x",  # same host, other port
        "http://crud.test/x",  # same host, other scheme
        "https://sub.crud.test/x",
        "https://crud.test.evil.example/x",
        "ftp://crud.test/x",  # not an http(s) origin at all
    ],
)
def test_the_token_is_refused_for_any_other_origin(url: str) -> None:
    """A direct `client.http.get` cannot carry the token off the trusted origins."""
    seen: list[httpx2.Request] = []
    with _client(seen) as client:
        with pytest.raises(OriginNotAllowedError) as info:
            client.http.get(url)
        assert isinstance(info.value, ClientError)
    assert seen == []


def test_the_default_port_is_the_same_origin_and_anonymous_requests_are_free() -> None:
    """`https://crud.test:443` is `https://crud.test`; without a token nothing is refused."""
    seen: list[httpx2.Request] = []
    with _client(seen) as client:
        assert client.http.get("https://crud.test:443/x").status_code == 200
    assert len(seen) == 1
    seen.clear()
    anonymous = CrudClient(
        BASE, transport=make_transport(lambda _r: httpx2.Response(200, json=[]), seen=seen)
    )
    with anonymous:
        assert anonymous.http.get(f"{EVIL}/x").status_code == 200


def test_allowed_origins_are_trusted_exactly() -> None:
    """An allowed origin receives the token; its other port still does not."""
    seen: list[httpx2.Request] = []
    with _client(seen, allowed_origins=["https://events.example.com:9443"]) as client:
        client.http.get("https://events.example.com:9443/e")
        with pytest.raises(OriginNotAllowedError):
            client.http.get("https://events.example.com/e")
    assert [str(r.url) for r in seen] == ["https://events.example.com:9443/e"]


def test_a_header_configured_on_the_client_is_covered_too() -> None:
    """A static Authorization header is a credential like the provider's token."""
    seen: list[httpx2.Request] = []
    client = CrudClient(
        BASE,
        headers={"Authorization": "Bearer fixed"},
        transport=make_transport(lambda _r: httpx2.Response(200, json=[]), seen=seen),
    )
    with client, pytest.raises(OriginNotAllowedError):
        client.http.get(f"{EVIL}/x")
    assert seen == []


def test_an_explicit_manifest_url_on_another_origin_must_be_allowed_when_a_token_is_sent() -> None:
    """A trusted-as-given manifest_url no longer carries the token to a foreign origin."""
    seen: list[httpx2.Request] = []
    refused = _client(seen, manifest_url=f"{EVIL}/m")
    with refused, pytest.raises(OriginNotAllowedError):
        refused.manifest()
    assert seen == []


def test_the_error_names_the_origin_without_path_or_query() -> None:
    """The message carries the origin only."""
    with _client([]) as client, pytest.raises(OriginNotAllowedError) as info:
        client.http.get(f"{EVIL}/secret/path?token=1")
    assert "https://evil.example" in str(info.value)
    assert "secret" not in str(info.value)
    assert "token=1" not in str(info.value)


@pytest.mark.asyncio
async def test_async_client_refuses_and_allows_the_same_way() -> None:
    """The async transport applies the same guard."""
    seen: list[httpx2.Request] = []
    async with AsyncCrudClient(
        BASE,
        token_provider=static_token("tok"),
        allowed_origins=[EVIL],
        transport=make_transport(lambda _r: httpx2.Response(200, json=[]), seen=seen),
    ) as client:
        assert (await client.http.get(f"{EVIL}/x")).status_code == 200
        with pytest.raises(OriginNotAllowedError):
            await client.http.get("https://other.example/x")
    assert len(seen) == 1


def test_helpers() -> None:
    """`check_origin(None)` is off; `trusted_origins` rejects a base that is no origin."""
    request = httpx2.Request("GET", f"{EVIL}/x", headers={"Authorization": "Bearer t"})
    check_origin(request, None)
    assert origin_of("http://a.example") == ("http", "a.example", 80)
    assert trusted_origins(BASE, [EVIL]) == {
        ("https", "crud.test", 443),
        ("https", "evil.example", 443),
    }
    for bad in ("not a url", "ftp://x.example", "http://\x00/"):
        with pytest.raises(ValueError, match="URL"):
            trusted_origins(bad)


@pytest.mark.parametrize(
    "header", ["Cookie", "X-Api-Key", "X-Auth-Token", "X-CSRF-Token", "X-Session-Id"]
)
def test_other_credential_headers_are_pinned_to_the_trusted_origins_too(header: str) -> None:
    """A cookie or API key configured on the client does not leave the trusted origins either."""
    seen: list[httpx2.Request] = []
    client = CrudClient(
        BASE,
        headers={header: "secret"},
        transport=make_transport(lambda _r: httpx2.Response(200, json=[]), seen=seen),
    )
    with client:
        assert client.http.get(f"{BASE}/x").status_code == 200
        with pytest.raises(OriginNotAllowedError):
            client.http.get(f"{EVIL}/x")
    assert len(seen) == 1


def test_a_base_url_that_is_not_an_http_origin_fails_at_construction() -> None:
    """Fail closed early: there is no origin to trust."""
    for bad in ("api.example.com", "localhost:8000", "ftp://x.example"):
        with pytest.raises(ValueError, match="URL"):
            CrudClient(bad)
