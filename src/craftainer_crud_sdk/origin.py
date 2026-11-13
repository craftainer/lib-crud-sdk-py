"""Same-origin guard for every URL the SDK joins onto `base_url`.

The token is attached to every request, so a manifest (or catalog) that names an absolute or
protocol-relative URL on another host must not make the client send it there.
"""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import urljoin

import httpx2

from craftainer_crud_sdk.errors import ManifestError

__all__ = ["Origin", "check_origins", "join_same_origin", "origin_of", "trusted_origins"]

_DEFAULT_PORTS = {"http": 80, "https": 443}


type Origin = tuple[str, str, int]
"""An origin as ``(scheme, host, port)``, the default port filled in (RFC 6454)."""


def origin_of(url: str | httpx2.URL) -> Origin:
    """The origin of an http(s) URL; `ValueError` (or `httpx2.InvalidURL`) if it has none."""
    parsed = httpx2.URL(url) if isinstance(url, str) else url
    if parsed.scheme not in _DEFAULT_PORTS or not parsed.host:
        raise ValueError(f"{str(url)!r} is not an http(s) URL with a host")
    return parsed.scheme, parsed.host, parsed.port or _DEFAULT_PORTS[parsed.scheme]


def trusted_origins(
    base_url: str, allowed_origins: Sequence[str] | None = None
) -> frozenset[Origin]:
    """The origins a credential may be sent to: `base_url`'s plus `allowed_origins`.

    Raises `ValueError` when `base_url` is not an http(s) URL with a host.
    """
    try:
        return frozenset({origin_of(base_url), *(origin_of(a) for a in allowed_origins or ())})
    except httpx2.InvalidURL as exc:
        raise ValueError(f"{base_url!r} is not a valid URL: {exc}") from exc


def check_origins(origins: Sequence[str] | None) -> tuple[str, ...]:
    """`allowed_origins` as a tuple; `ValueError` for an entry that is no http(s) origin."""
    checked = tuple(origins or ())
    for origin in checked:
        try:
            origin_of(origin)
        except (httpx2.InvalidURL, ValueError) as exc:
            raise ValueError(f"allowed_origins entry {origin!r} is not an origin: {exc}") from exc
    return checked


def join_same_origin(base: str, ref: str, allowed_origins: Sequence[str] | None = None) -> str:
    """`urljoin(base, ref)`, refusing a result whose origin is not `base`'s or an allowed one.

    `allowed_origins` are origins such as `https://events.example.com` (scheme, host, port; a
    path is ignored). Raises `ManifestError` for an absolute or protocol-relative `ref` that
    resolves to any other origin.
    """
    try:
        joined = urljoin(base, ref)
        origin = origin_of(joined)
        trusted = {origin_of(base), *(origin_of(a) for a in allowed_origins or ())}
    except (httpx2.InvalidURL, ValueError) as exc:
        raise ManifestError(f"{ref!r} is not a valid URL to join onto {base!r}: {exc}") from exc
    if origin not in trusted:
        raise ManifestError(
            f"{ref!r} resolves to {joined!r}, which is not on the origin of {base!r}; "
            "list its origin in allowed_origins to permit it"
        )
    return joined
