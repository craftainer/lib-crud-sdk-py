"""OF1: exceptions and log lines carry no credentials, query values or unbounded server text."""

import logging

import fakeredis
import httpx2
import pytest

from craftainer_crud_sdk import CrudClient, NotFound, ResponseError
from craftainer_crud_sdk.cache.keys import build_key, log_key
from craftainer_crud_sdk.cache.redis import RedisStore
from craftainer_crud_sdk.errors import (
    MAX_MESSAGE_CHARS,
    REDACTED,
    ProblemError,
    problem_from_response,
    redact_headers,
    redact_response,
    safe_url,
)

from ._support import BASE, make_transport


def _response(**kwargs: object) -> httpx2.Response:
    request = httpx2.Request(
        "GET",
        "https://user:pw@api.test/heroes?name=Ada&ssn=123#frag",
        headers={"Authorization": "Bearer SECRET", "X-Api-Key": "k", "Accept": "application/json"},
        content=b'{"private": 1}',
    )
    return httpx2.Response(
        404,
        request=request,
        headers={"Set-Cookie": "sid=abc", "Retry-After": "3", "X-Session-Id": "s"},
        json={"title": "Gone", "detail": "no such row"},
        **kwargs,  # type: ignore[arg-type]
    )


def test_the_response_on_an_exception_has_no_credentials() -> None:
    """Request headers, query, userinfo and request body are not reachable from the exception."""
    error = problem_from_response(_response())
    assert isinstance(error, NotFound)
    assert error.response is not None
    request = error.response.request
    assert request.headers["authorization"] == REDACTED
    assert request.headers["x-api-key"] == REDACTED
    assert request.headers["accept"] == "application/json"
    assert str(request.url) == "https://api.test/heroes"
    assert request.content == b""
    assert error.response.headers["set-cookie"] == REDACTED
    assert error.headers["set-cookie"] == REDACTED
    assert error.headers["retry-after"] == "3"  # not a secret, still usable
    assert error.response.json()["title"] == "Gone"
    assert "SECRET" not in repr(vars(error)) + repr(vars(error.response.request))


def test_response_error_redacts_and_bounds_its_message() -> None:
    """`ResponseError.response` is the redacted copy; its message is bounded."""
    error = ResponseError("x" * 5000, _response())
    assert error.response is not None
    assert error.response.request.headers["authorization"] == REDACTED
    assert len(str(error)) < 4 * MAX_MESSAGE_CHARS + 10
    assert error.status == 404
    assert ResponseError("plain").response is None


def test_problem_message_clips_server_text() -> None:
    """Title, detail and per-field messages are clipped."""
    long = "y" * 1000
    assert len(str(ProblemError(400, long, long))) < 2 * MAX_MESSAGE_CHARS + 20
    many = tuple({"loc": ["a"], "msg": long} for _ in range(10))
    assert len(str(ProblemError(422, "Bad", None, errors=many))) < 5 * MAX_MESSAGE_CHARS + 20


def test_redact_helpers() -> None:
    """Plain mappings and repeated headers are handled; odd URLs do not raise."""
    out = redact_headers({"Cookie": "a", "Accept": "x"})
    assert out["cookie"] == REDACTED
    assert out["accept"] == "x"
    multi = redact_headers(httpx2.Headers([("Set-Cookie", "a"), ("Set-Cookie", "b")]))
    assert multi.get_list("set-cookie") == [REDACTED, REDACTED]
    assert safe_url("https://a.test/p?q=1") == "https://a.test/p"
    assert safe_url("http://\x00/") == "<unparseable url>"


def test_redact_response_without_request_or_read_body() -> None:
    """A response with no request, an unread stream and a content-encoding is still copied."""
    bare = httpx2.Response(200, headers={"Content-Encoding": "gzip", "Content-Length": "9"})
    copy = redact_response(bare)
    assert copy.status_code == 200
    assert "content-encoding" not in copy.headers
    unread = httpx2.Response(200, stream=httpx2.ByteStream(b"abc"))
    assert redact_response(unread).content == b""


def test_log_key_drops_scope_and_query() -> None:
    """Only `v1/resource/kind` and a digest remain."""
    key = build_key("heroes", "list", None, "a" * 32, "ssn=123-45", api_version="v2")
    shown = log_key(key)
    assert shown.startswith("v1/heroes/list/#")
    assert "a" * 32 not in shown
    assert "ssn" not in shown
    assert log_key(key) == shown
    assert log_key(key + "x") != shown


def test_redis_corrupt_value_logs_no_key_material(caplog: pytest.LogCaptureFixture) -> None:
    """The warning names a digest, not the scope or the filter values."""
    client = fakeredis.FakeRedis()
    key = build_key("heroes", "list", None, "f" * 32, "ssn=123-45", api_version="v2")
    client.set("craftainer-crud:" + key, b"garbage")
    with caplog.at_level(logging.WARNING):
        assert RedisStore(client).get(key) is None
    assert "corrupt" in caplog.text
    assert "f" * 32 not in caplog.text
    assert "123-45" not in caplog.text


def test_a_redirect_location_is_shown_without_its_query() -> None:
    """The message names the Location's path only."""
    redirect = httpx2.Response(302, headers={"Location": "https://other.test/next?code=SECRET"})
    with (
        CrudClient(BASE, transport=make_transport(lambda _r: redirect)) as client,
        pytest.raises(ResponseError) as info,
    ):
        client.resource("heroes").get(1)
    assert "https://other.test/next" in str(info.value)
    assert "SECRET" not in str(info.value)
