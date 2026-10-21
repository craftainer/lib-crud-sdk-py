"""Exception hierarchy: client-side errors and RFC 9457 problem details."""

from __future__ import annotations

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
    "Forbidden",
    "InvalidQueryError",
    "Locked",
    "ManifestError",
    "NotFound",
    "PreconditionFailed",
    "ProblemError",
    "RateLimited",
    "ResponseError",
    "ServerError",
    "Unauthorized",
    "UnknownResourceError",
    "ValidationFailed",
    "problem_from_response",
]

_NO_TYPE = "about:blank"


class CrudError(Exception):
    """Base class of every error this package raises on purpose."""


class ClientError(CrudError):
    """The call was refused before any request was sent."""


class CapabilityError(ClientError):
    """The resource does not support the operation (manifest capability or path missing)."""


class InvalidQueryError(ClientError):
    """A filter, sort or paging argument is not valid for the resource."""


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
        super().__init__(message)
        self.response = response
        self.status = None if response is None else response.status_code


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
        message = f"{status} {title}"
        if detail:
            message += f": {detail}"
        elif errors:
            message += ": " + "; ".join(
                f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in errors
            )
        super().__init__(message)
        self.status = status
        self.title = title
        self.detail = detail
        self.type = type
        self.instance = instance
        self.errors = errors
        self.headers: Mapping[str, str] = headers if headers is not None else {}
        self.response = response


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
        """Seconds to wait, or `None` when absent or an HTTP date."""
        raw = self.headers.get("retry-after")
        try:
            return None if raw is None else float(raw)
        except ValueError:
            return None


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
