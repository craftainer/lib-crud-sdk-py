"""Tests for the problem-details exception hierarchy."""

import httpx2
import pytest

from craftainer_crud_sdk import (
    BadRequest,
    CrudError,
    Forbidden,
    Locked,
    NotFound,
    PreconditionFailed,
    ProblemError,
    RateLimited,
    ServerError,
    Unauthorized,
    ValidationFailed,
    problem_from_response,
)


@pytest.mark.parametrize(
    ("status", "cls"),
    [
        (400, BadRequest),
        (401, Unauthorized),
        (403, Forbidden),
        (404, NotFound),
        (412, PreconditionFailed),
        (422, ValidationFailed),
        (423, Locked),
        (429, RateLimited),
        (500, ServerError),
        (503, ServerError),
        (409, ProblemError),
    ],
)
def test_status_maps_to_class(status: int, cls: type[ProblemError]) -> None:
    """Test status maps to class."""
    error = problem_from_response(httpx2.Response(status, json={"title": "T", "detail": "D"}))
    assert type(error) is cls
    assert isinstance(error, CrudError)
    assert (error.status, error.title, error.detail) == (status, "T", "D")
    assert str(error) == f"{status} T: D"


def test_full_problem_members() -> None:
    """Test full problem members."""
    response = httpx2.Response(
        404,
        json={"type": "https://e/x", "title": "Gone", "instance": "/a/1", "detail": "nope"},
    )
    error = problem_from_response(response)
    assert (error.type, error.instance, error.response) == ("https://e/x", "/a/1", response)
    assert error.errors == ()


def test_non_json_body_is_synthesized() -> None:
    """Test non json body is synthesized."""
    error = problem_from_response(httpx2.Response(502, text="<html>bad gateway</html>"))
    assert isinstance(error, ServerError)
    assert (error.title, error.detail, error.type) == ("Bad Gateway", None, "about:blank")


def test_json_array_body_is_synthesized() -> None:
    """Test json array body is synthesized."""
    error = problem_from_response(httpx2.Response(404, json=[1]))
    assert (error.title, error.detail, error.instance) == ("Not Found", None, None)


def test_unknown_status_phrase() -> None:
    """Test unknown status phrase."""
    assert problem_from_response(httpx2.Response(499)).title == "Error"


def test_validation_errors_are_normalized() -> None:
    """Test validation errors are normalized."""
    body = {
        "detail": [
            {"loc": ["body", "name"], "msg": "Field required", "type": "missing"},
            {"loc": ("query", 0), "msg": "bad", "type": "value_error", "input": 1},
            {"msg": "no loc"},
            "plain",
        ]
    }
    error = problem_from_response(httpx2.Response(422, json=body))
    assert isinstance(error, ValidationFailed)
    assert error.detail is None
    assert error.errors == (
        {"loc": ["body", "name"], "msg": "Field required", "type": "missing"},
        {"loc": ["query", 0], "msg": "bad", "type": "value_error"},
        {"loc": [], "msg": "no loc", "type": ""},
        {"loc": [], "msg": "plain", "type": ""},
    )
    assert str(error).startswith("422 Unprocessable Content: body.name: Field required;")


def test_rfc9457_errors_member() -> None:
    """Test rfc9457 errors member."""
    body = {"title": "Invalid", "errors": [{"loc": ["x"], "msg": "m", "type": "t"}]}
    error = problem_from_response(httpx2.Response(422, json=body))
    assert error.errors == ({"loc": ["x"], "msg": "m", "type": "t"},)


def test_unauthorized_keeps_challenge() -> None:
    """Test unauthorized keeps challenge."""
    response = httpx2.Response(401, headers={"WWW-Authenticate": 'Bearer realm="x"'})
    error = problem_from_response(response)
    assert isinstance(error, Unauthorized)
    assert error.www_authenticate == 'Bearer realm="x"'
    assert error.headers["www-authenticate"] == 'Bearer realm="x"'


def test_rate_limited_retry_after() -> None:
    """Test rate limited retry after."""

    def make(headers: dict[str, str]) -> RateLimited:
        """Make."""
        error = problem_from_response(httpx2.Response(429, headers=headers))
        assert isinstance(error, RateLimited)
        return error

    assert make({"Retry-After": "7"}).retry_after == 7.0
    assert make({}).retry_after is None
    assert make({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}).retry_after is None


def test_headers_default_empty() -> None:
    """Test headers default empty."""
    assert ProblemError(400, "x").headers == {}
