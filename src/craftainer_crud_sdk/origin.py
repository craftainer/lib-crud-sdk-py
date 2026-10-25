"""Same-origin guard for every URL the SDK joins onto `base_url`.

The token is attached to every request, so a manifest (or catalog) that names an absolute or
protocol-relative URL on another host must not make the client send it there.
"""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import urljoin

import httpx2

from craftainer_crud_sdk.errors import ManifestError

__all__ = ["join_same_origin"]

_DEFAULT_PORTS = {"http": 80, "https": 443}


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = httpx2.URL(url)
    return parsed.scheme, parsed.host, parsed.port or _DEFAULT_PORTS.get(parsed.scheme)


def join_same_origin(base: str, ref: str, allowed_origins: Sequence[str] | None = None) -> str:
    """`urljoin(base, ref)`, refusing a result whose origin is not `base`'s or an allowed one.

    `allowed_origins` are origins such as `https://events.example.com` (scheme, host, port; a
    path is ignored). Raises `ManifestError` for an absolute or protocol-relative `ref` that
    resolves to any other origin.
    """
    try:
        joined = urljoin(base, ref)
        origin = _origin(joined)
        trusted = {_origin(base), *(_origin(a) for a in allowed_origins or ())}
    except (httpx2.InvalidURL, ValueError) as exc:
        raise ManifestError(f"{ref!r} is not a valid URL to join onto {base!r}: {exc}") from exc
    if origin not in trusted:
        raise ManifestError(
            f"{ref!r} resolves to {joined!r}, which is not on the origin of {base!r}; "
            "list its origin in allowed_origins to permit it"
        )
    return joined
