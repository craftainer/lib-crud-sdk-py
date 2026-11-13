"""Exception hierarchy: client-side errors and RFC 9457 problem details."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from http import HTTPStatus
from typing import Any

import httpx2

__all__ = [
    "BadRequest",
    "CapabilityError",
    "ClientError",
    "ConnectionFailed",
    "CrudError",
    "DeadlineExceededError",
    "Forbidden",
    "InsecureTransportError",
    "InvalidQueryError",
    "Locked",
    "ManifestError",
    "NotFound",
    "OriginNotAllowedError",
    "PaginationLimitExceededError",
    "PreconditionFailed",
    "ProblemError",
    "RateLimited",
    "ResponseError",
    "ResponseTooLargeError",
    "ServerError",
    "Unauthorized",
    "UnknownResourceError",
    "ValidationFailed",
    "is_credential_header",
    "problem_from_response",
    "redact_headers",
    "redact_response",
    "safe_url",
]

_NO_TYPE = "about:blank"

MAX_MESSAGE_CHARS = 300
"""Longest server-supplied text (title, detail, field message) copied into an exception message."""

REDACTED = "[redacted]"

_SENSITIVE_HEADER = re.compile(
    r"authorization|cookie|token|secret|api[-_]?key|csrf|xsrf|session|credential", re.IGNORECASE
)
FRAMING_HEADERS = frozenset({"content-encoding", "content-length", "transfer-encoding"})


def is_credential_header(name: str) -> bool:
    """Whether a header name carries a credential (authorization, cookie, token, key, session)."""
    return _SENSITIVE_HEADER.search(name) is not None


def _clip(text: str, limit: int = MAX_MESSAGE_CHARS) -> str:
    """`text` cut to `limit` characters (an ellipsis marks the cut)."""
    return text if len(text) <= limit else text[:limit] + "..."


def safe_url(url: str | httpx2.URL) -> str:
    """`url` without userinfo, query and fragment (they carry credentials and filter values)."""
    try:
        parsed = httpx2.URL(url) if isinstance(url, str) else url
        return str(parsed.copy_with(username=None, password=None, query=None, fragment=None))
    except httpx2.InvalidURL, ValueError:
        return "<unparseable url>"


def redact_headers(headers: Mapping[str, str] | httpx2.Headers) -> httpx2.Headers:
    """A copy of `headers` with the value of every credential-bearing header replaced.

    Names matching authorization, cookie, token, secret, api key, csrf/xsrf, session or credential
    (case-insensitive) are redacted, repeated headers included.
    """
    items = headers.multi_items() if isinstance(headers, httpx2.Headers) else list(headers.items())
    return httpx2.Headers([(k, REDACTED if is_credential_header(k) else v) for k, v in items])


def redact_response(response: httpx2.Response) -> httpx2.Response:
    """A copy of `response` that is safe to keep on an exception.

    The original keeps `request.headers` (so the ``Authorization`` token) and the request URL's
    query; an error reporter that serialises exception attributes would ship them. (Frame locals
    of the SDK's own frames are outside what this can protect; `elapsed` is not copied.) The
    copy keeps the status, the (redacted) headers, the body and the request method and URL
    without userinfo, query or fragment; the request headers are redacted and its body dropped.
    """
    try:
        content = response.content
    except httpx2.ResponseNotRead:
        content = b""
    headers = [
        (k, v) for k, v in redact_headers(response.headers).multi_items()
        if k.lower() not in FRAMING_HEADERS
    ]  # fmt: skip
    try:
        request: httpx2.Request | None = response.request
    except RuntimeError:  # a response built without a request
        request = None
    copy = httpx2.Response(response.status_code, headers=headers, content=content)
    if request is not None:
        copy.request = httpx2.Request(
            request.method, safe_url(request.url), headers=redact_headers(request.headers)
        )
    return copy


class CrudError(Exception):
    """Base class of every error this package raises on purpose."""


class ClientError(CrudError):
    """The call was refused before any request was sent."""


class CapabilityError(ClientError):
    """The resource does not support the operation (manifest capability or path missing)."""


class InvalidQueryError(ClientError):
    """A filter, sort or paging argument is not valid for the resource."""


class InsecureTransportError(ClientError):
    """A request carrying an ``Authorization`` header was about to go over plain ``http:`` to a
    non-loopback host. Nothing was sent. Use ``https:``, or opt in with ``allow_insecure_http``.
    """


class OriginNotAllowedError(ClientError):
    """A request carrying an ``Authorization`` header was about to go to an origin outside
    `base_url` and `allowed_origins`. Nothing was sent. List the origin in ``allowed_origins``.
    """


class ManifestError(ClientError):
    """The discovery document or the manifest is missing, unusable or of an unknown version."""


class UnknownResourceError(ClientError):
    """The manifest lists no such resource for this caller."""


class ConnectionFailed(CrudError):  # noqa: N818 - name fixed by the SDK spec
    """The request never produced a response; `__cause__` holds the transport error."""


class ResponseError(CrudError):
    """A success response whose body is not the JSON the client expected.

    `__cause__` holds the parse error; `response` is the offending response.
    """

    def __init__(self, message: str, response: httpx2.Response | None = None) -> None:
        """Keep the response next to the message."""
        super().__init__(_clip(message, 4 * MAX_MESSAGE_CHARS))
        self.response = None if response is None else redact_response(response)
        self.status = None if response is None else response.status_code


class ResponseTooLargeError(ResponseError):
    """A response body is larger than the client's `max_response_bytes`; reading stopped."""


class DeadlineExceededError(CrudError):
    """A request did not finish within the client's `total_timeout`; reading stopped."""


class PaginationLimitExceededError(CrudError):
    """`iter_all` reached `max_pages` (or the server ignored `limit`) before the last page."""


class ProblemError(CrudError):
    """An error response, as RFC 9457 problem details."""

    def __init__(  # noqa: PLR0913, PLR0917 - mirrors the RFC 9457 members
        self,
        status: int,
        title: str,
        detail: str | None = None,
        type: str = _NO_TYPE,  # noqa: A002
        instance: str | None = None,
        errors: tuple[Mapping[str, Any], ...] = (),
        headers: Mapping[str, str] | None = None,
        response: httpx2.Response | None = None,
    ) -> None:
        """Keep the members and build a readable message."""
        message = f"{status} {_clip(title)}"
        if detail:
            message += f": {_clip(detail)}"
        elif errors:
            message += ": " + _clip(
                "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in errors),
                4 * MAX_MESSAGE_CHARS,
            )
        super().__init__(message)
        self.status = status
        self.title = title
        self.detail = detail
        self.type = type
        self.instance = instance
        self.errors = errors
        self.headers: Mapping[str, str] = {} if headers is None else redact_headers(headers)
        self.response = None if response is None else redact_response(response)


class BadRequest(ProblemError):  # noqa: N818 - name fixed by the SDK spec
    """400."""


class Unauthorized(ProblemError):  # noqa: N818 - name fixed by the SDK spec
    """401; `www_authenticate` keeps the challenge."""

    @property
    def www_authenticate(self) -> str | None:
        """The `WWW-Authenticate` response header, if any."""
        return self.headers.get("www-authenticate")


class Forbidden(ProblemError):  # noqa: N818 - name fixed by the SDK spec
    """403."""


class NotFound(ProblemError):  # noqa: N818 - name fixed by the SDK spec
    """404."""


class PreconditionFailed(ProblemError):  # noqa: N818 - name fixed by the SDK spec
    """412: a stale `If-Match`."""


class ValidationFailed(ProblemError):  # noqa: N818 - name fixed by the SDK spec
    """422; `errors` holds normalized `{loc, msg, type}` entries."""


class Locked(ProblemError):  # noqa: N818 - name fixed by the SDK spec
    """423."""


class RateLimited(ProblemError):  # noqa: N818 - name fixed by the SDK spec
    """429; `retry_after` is the `Retry-After` header in seconds, when it is a number."""

    @property
    def retry_after(self) -> float | None:
        """Seconds to wait, or `None` when absent, an HTTP date, or not a finite number >= 0."""
        raw = self.headers.get("retry-after")
        try:
            seconds = None if raw is None else float(raw)
        except ValueError:
            return None
        return seconds if seconds is not None and math.isfinite(seconds) and seconds >= 0 else None


class ServerError(ProblemError):
    """5xx."""


_BY_STATUS: dict[int, type[ProblemError]] = {
    400: BadRequest,
    401: Unauthorized,
    403: Forbidden,
    404: NotFound,
    412: PreconditionFailed,
    422: ValidationFailed,
    423: Locked,
    429: RateLimited,
}


def _normalize_errors(items: list[Any]) -> tuple[Mapping[str, Any], ...]:
    out: list[Mapping[str, Any]] = []
    for item in items:
        if isinstance(item, dict):
            loc = item.get("loc")
            out.append(
                {
                    "loc": list(loc) if isinstance(loc, list | tuple) else [],
                    "msg": str(item.get("msg", "")),
                    "type": str(item.get("type", "")),
                }
            )
        else:
            out.append({"loc": [], "msg": str(item), "type": ""})
    return tuple(out)


def problem_from_response(response: httpx2.Response) -> ProblemError:
    """Build the matching `ProblemError` subclass from an error response.

    A body that is not a JSON object yields a synthesized problem from the status phrase.
    """
    status = response.status_code
    try:
        phrase = HTTPStatus(status).phrase
    except ValueError:
        phrase = "Error"
    try:
        body: Any = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        body = {}
    raw_detail = body.get("detail")
    raw_errors = raw_detail if isinstance(raw_detail, list) else body.get("errors")
    cls = _BY_STATUS.get(status) or (ServerError if status >= 500 else ProblemError)  # noqa: PLR2004
    return cls(
        status,
        str(body.get("title") or phrase),
        raw_detail if isinstance(raw_detail, str) else None,
        str(body.get("type") or _NO_TYPE),
        body.get("instance") if isinstance(body.get("instance"), str) else None,
        _normalize_errors(raw_errors) if isinstance(raw_errors, list) else (),
        response.headers,
        response,
    )
