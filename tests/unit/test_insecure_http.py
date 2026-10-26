"""SDKPY3: a bearer token is refused over plain http except to loopback, before any send."""

import httpx2
import pytest

from craftainer_crud_sdk import (
    AsyncCrudClient,
    ClientError,
    ConnectionFailed,
    CrudClient,
    HookedTransport,
    InsecureTransportError,
    static_token,
)
from craftainer_crud_sdk.auth import BearerAuth, is_insecure_remote

from ._support import MANIFEST_PATH, load_doc, make_transport

LOOPBACK = [
    "http://localhost",
    "http://localhost:8080/x",
    "http://LOCALHOST./x",
    "http://api.localhost",
    "http://a.b.localhost:3000",
    "http://127.0.0.1",
    "http://127.255.255.254:9",
    "http://[::1]:8000",
    "https://api.example.com",
    "https://10.0.0.5",
    "ftp://example.com",
]
REMOTE = [
    "http://api.example.com",
    "http://localhost.evil.example",
    "http://evil-localhost",
    "http://localhost@evil.example",
    "http://127.0.0.1.evil.example",
    "http://128.0.0.1",
    "http://10.0.0.5:8080",
    "http://[::2]",
    "http://[::ffff:8.8.8.8]",
    "http://[::ffff:127.0.0.1]",
]


@pytest.mark.parametrize("url", LOOPBACK)
def test_allowed_urls(url: str) -> None:
    """https, other schemes and loopback http are not insecure."""
    assert not is_insecure_remote(httpx2.URL(url))


@pytest.mark.parametrize("url", REMOTE)
def test_flagged_urls(url: str) -> None:
    """Plain http to anything else is."""
    assert is_insecure_remote(httpx2.URL(url))


def _client(origin: str, **kwargs: object) -> tuple[CrudClient, list[httpx2.Request]]:
    seen: list[httpx2.Request] = []
    kwargs.setdefault("token_provider", static_token("tok"))
    client = CrudClient(
        origin,
        manifest_url=f"{origin}{MANIFEST_PATH}",
        transport=make_transport(lambda _r: httpx2.Response(404), seen=seen),
        **kwargs,  # type: ignore[arg-type]
    )
    return client, seen


def test_refuses_remote_http_before_sending() -> None:
    """The error names the origin and the opt-in; nothing reaches the transport."""
    client, seen = _client("http://api.example.com:8080")
    with pytest.raises(InsecureTransportError) as info:
        client.manifest()
    assert isinstance(info.value, ClientError)
    assert not isinstance(info.value, ConnectionFailed)
    assert "http://api.example.com:8080" in str(info.value)
    assert "allow_insecure_http" in str(info.value)
    assert seen == []


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:8000",
        "http://app.localhost",
        "http://127.0.0.1:8000",
        "http://127.9.9.9",
        "http://[::1]:8000",
        "https://api.example.com",
    ],
)
def test_sends_the_token_to_loopback_and_https(origin: str) -> None:
    """Loopback http and any https carry the token."""
    client, seen = _client(origin)
    client.manifest()
    assert seen[0].headers["authorization"] == "Bearer tok"


def test_opt_in_allows_remote_http() -> None:
    """allow_insecure_http=True sends the token."""
    client, seen = _client("http://api.example.com", allow_insecure_http=True)
    client.manifest()
    assert seen[0].headers["authorization"] == "Bearer tok"


@pytest.mark.parametrize("provider", [None, static_token(None), static_token("")])
def test_no_token_is_not_checked(provider: object) -> None:
    """Anonymous requests still go over plain http."""
    client, seen = _client("http://api.example.com", token_provider=provider)
    client.manifest()
    assert "authorization" not in seen[0].headers


def test_configured_authorization_header_is_checked() -> None:
    """A header set on the client counts as a credential too."""
    client, seen = _client(
        "http://api.example.com", token_provider=None, headers={"Authorization": "Basic abc"}
    )
    with pytest.raises(InsecureTransportError):
        client.manifest()
    assert seen == []


def test_send_authenticated_is_checked() -> None:
    """A hook's background request (refresh/SWR path) goes through the same check."""
    seen: list[httpx2.Request] = []

    transport = HookedTransport(
        make_transport(lambda _r: httpx2.Response(200), seen=seen),
        [],
        auth=BearerAuth(static_token("tok")),
    )
    with pytest.raises(InsecureTransportError):
        transport.send_authenticated(httpx2.Request("GET", "http://api.example.com/x"))
    assert seen == []
    transport.send_authenticated(httpx2.Request("GET", "http://localhost/x"))
    assert seen[0].headers["authorization"] == "Bearer tok"


def test_hook_rewriting_the_url_is_checked() -> None:
    """The check sees the request as sent, after hooks changed it."""

    class Rewrite:
        def on_request(self, request: httpx2.Request) -> None:
            request.url = httpx2.URL("http://elsewhere.example/m")

        def on_response(
            self, request: httpx2.Request, response: httpx2.Response
        ) -> httpx2.Response:
            return response

    client, seen = _client("https://api.test", hooks=[Rewrite()])
    with pytest.raises(InsecureTransportError):
        client.manifest()
    assert seen == []


@pytest.mark.asyncio
async def test_async_client_parity() -> None:
    """The async client refuses, allows loopback, and honors the opt-in."""
    seen: list[httpx2.Request] = []

    def make(origin: str, **kw: object) -> AsyncCrudClient:
        return AsyncCrudClient(
            origin,
            manifest_url=f"{origin}{MANIFEST_PATH}",
            token_provider=static_token("tok"),
            transport=make_transport(lambda _r: httpx2.Response(404), seen=seen),
            **kw,  # type: ignore[arg-type]
        )

    async with make("http://api.example.com") as client:
        with pytest.raises(InsecureTransportError):
            await client.manifest()
    assert seen == []
    async with make("http://localhost:1") as client:
        await client.manifest()
    async with make("http://api.example.com", allow_insecure_http=True) as client:
        await client.manifest()
    assert len(seen) == 2
    assert load_doc()  # fixture still loads
