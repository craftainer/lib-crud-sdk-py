"""Turning responses into `CacheEntry` values and back, and the Redis wire format."""

import base64
import json
from collections.abc import Iterable, Mapping

import httpx2

from craftainer_crud_sdk.interfaces import CacheEntry

__all__ = [
    "CACHE_STATUS_EXTENSION",
    "build_entry",
    "cache_control",
    "entry_from_json",
    "entry_to_json",
    "is_json",
    "merge_headers",
    "to_response",
    "vary_allows_caching",
]

CACHE_STATUS_EXTENSION = "craftainer_cache"
"""`response.extensions[...]` says how the cache answered: hit, stale, revalidated or miss."""

# Describe the transfer, not the representation: the stored body is already decoded.
_TRANSFER_HEADERS = frozenset(
    {"content-length", "content-encoding", "transfer-encoding", "connection", "keep-alive"}
)
_EXCHANGE_HEADERS = frozenset({"set-cookie"})
"""Belong to the exchange that carried them: a replay would re-set the client's cookie jar."""
_VARY_OK = frozenset({"authorization", "accept", "accept-encoding"})


def cache_control(headers: httpx2.Headers) -> frozenset[str]:
    """The lower-cased directive names of every ``Cache-Control`` header."""
    names = {
        part.split("=", 1)[0].strip().lower()
        for value in headers.get_list("cache-control")
        for part in value.split(",")
    }
    names.discard("")
    return frozenset(names)


def is_json(response: httpx2.Response) -> bool:
    """Whether the response media type is JSON (``application/json`` or ``+json``)."""
    media = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    return media == "application/json" or media.endswith("+json")


def vary_allows_caching(response: httpx2.Response) -> bool:
    """``Vary`` may only name headers the cache key already accounts for; ``*`` never caches."""
    names = {
        part.strip().lower()
        for value in response.headers.get_list("vary")
        for part in value.split(",")
    }
    names.discard("")
    return names <= _VARY_OK


def _storable_headers(items: Iterable[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    skipped = _TRANSFER_HEADERS | _EXCHANGE_HEADERS
    return tuple((k, v) for k, v in items if k.lower() not in skipped)


def build_entry(
    response: httpx2.Response,
    *,
    now: float,
    fresh: float,
    stale: float,
    keep_etag: bool,
) -> CacheEntry:
    """Snapshot a read response. Call after the body was read; ``stale`` extends ``fresh``."""
    etag = response.headers.get("etag") if keep_etag else None
    return CacheEntry(
        status=response.status_code,
        headers=_storable_headers(response.headers.multi_items()),
        body=response.content,
        stored_at=now,
        fresh_until=now + fresh,
        stale_until=now + fresh + stale,
        etag=etag,
    )


def merge_headers(
    stored: tuple[tuple[str, str], ...], update: httpx2.Headers
) -> tuple[tuple[str, str], ...]:
    """RFC 9111 4.3.4: headers of a ``304`` replace the stored ones of the same name."""
    fresh_items = _storable_headers(update.multi_items())
    replaced = {k.lower() for k, _ in fresh_items}
    kept = tuple((k, v) for k, v in stored if k.lower() not in replaced)
    return (*kept, *fresh_items)


_WIRE_EXTENSIONS = frozenset({"reason_phrase", "http_version", "network_stream"})
"""Extensions that describe one HTTP exchange (a `304`'s), not the rebuilt `200`."""


def to_response(
    entry: CacheEntry,
    request: httpx2.Request,
    *,
    status: str,
    extensions: Mapping[str, object],
    set_cookies: Iterable[str] = (),
) -> httpx2.Response:
    """Rebuild a response from an entry, tagged with how the cache produced it.

    Extensions that describe the exchange itself (reason phrase, HTTP version, network stream)
    are not copied from `extensions`: they belong to the response being replaced. Entries hold no
    ``Set-Cookie``; ``set_cookies`` are those of the live exchange being rebuilt (a ``304``).
    """
    kept = {k: v for k, v in extensions.items() if k not in _WIRE_EXTENSIONS}
    return httpx2.Response(
        entry.status,
        headers=[*entry.headers, *(("set-cookie", cookie) for cookie in set_cookies)],
        content=entry.body,
        request=request,
        extensions={**kept, CACHE_STATUS_EXTENSION: status},
    )


def entry_to_json(entry: CacheEntry) -> bytes:
    """Serialise for Redis: JSON with a base64 body."""
    return json.dumps(
        {
            "status": entry.status,
            "headers": [list(pair) for pair in entry.headers],
            "body": base64.b64encode(entry.body).decode("ascii"),
            "stored_at": entry.stored_at,
            "fresh_until": entry.fresh_until,
            "stale_until": entry.stale_until,
            "etag": entry.etag,
        },
        separators=(",", ":"),
    ).encode()


def entry_from_json(raw: bytes | str) -> CacheEntry:
    """Inverse of `entry_to_json`; raises ``ValueError``/``KeyError``/``TypeError`` if corrupt."""
    data = json.loads(raw)
    return CacheEntry(
        status=int(data["status"]),
        headers=tuple((str(k), str(v)) for k, v in data["headers"]),
        body=base64.b64decode(data["body"], validate=True),
        stored_at=float(data["stored_at"]),
        fresh_until=float(data["fresh_until"]),
        stale_until=float(data["stale_until"]),
        etag=data["etag"],
    )
