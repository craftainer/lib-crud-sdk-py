"""Redis cache stores. Importing this module needs the ``redis`` extra.

Values are JSON with a base64 body, written with ``SET ... PX``. ``clear`` only ever touches keys
under the store's namespace (``SCAN`` plus ``UNLINK`` in batches, never ``FLUSHDB``). An outage
or a corrupt value is a miss and a logged warning; it never fails the request.
"""

import logging
import os.path
from collections.abc import Iterable, Sequence

import redis as redis_sync
import redis.asyncio as redis_async

from craftainer_crud_sdk.cache.entry import entry_from_json, entry_to_json
from craftainer_crud_sdk.interfaces import CacheEntry

__all__ = ["AsyncRedisStore", "RedisStore"]

log = logging.getLogger(__name__)

DEFAULT_NAMESPACE = "craftainer-crud:"
DEFAULT_TTL = 3600.0
_BATCH = 500
_MISSING_OR_CORRUPT = (ValueError, KeyError, TypeError)


def _glob_escape(text: str) -> str:
    """Escape Redis glob metacharacters so a prefix matches literally."""
    return "".join(f"\\{c}" if c in "\\*?[]" else c for c in text)


def _px(ttl: float | None, default: float) -> int:
    return max(int((default if ttl is None else ttl) * 1000), 1)


def _batches(keys: Iterable[bytes | str]) -> Iterable[list[bytes | str]]:
    batch: list[bytes | str] = []
    for key in keys:
        batch.append(key)
        if len(batch) >= _BATCH:
            yield batch
            batch = []
    if batch:
        yield batch


def _scan_pattern(namespace: str, prefixes: Sequence[str]) -> str:
    """Glob matching every key under any prefix: their longest common prefix, then ``*``."""
    return _glob_escape(os.path.commonprefix([namespace + p for p in prefixes])) + "*"


def _under(key: bytes | str, owned: tuple[str, ...]) -> bool:
    return (key.decode() if isinstance(key, bytes) else key).startswith(owned)


class RedisStore:
    """`CacheStore` over a synchronous ``redis.Redis`` client (any ``decode_responses``)."""

    def __init__(
        self,
        client: redis_sync.Redis,
        *,
        namespace: str = DEFAULT_NAMESPACE,
        default_ttl: float = DEFAULT_TTL,
    ) -> None:
        """Use ``client``; every key is prefixed with ``namespace``."""
        self._client = client
        self._namespace = namespace
        self._default_ttl = default_ttl

    def get(self, key: str) -> CacheEntry | None:
        """Return the entry; a Redis error or a corrupt value is a miss."""
        try:
            raw = self._client.get(self._namespace + key)
        except redis_sync.RedisError as exc:
            log.warning("redis get failed, treating as a cache miss: %s", exc)
            return None
        if raw is None:
            return None
        try:
            return entry_from_json(raw)
        except _MISSING_OR_CORRUPT as exc:
            log.warning("corrupt cache value for %s, treating as a miss: %s", key, exc)
            return None

    def set(self, key: str, entry: CacheEntry, ttl: float | None = None) -> None:
        """Store ``entry``; a Redis error is logged and ignored."""
        try:
            self._client.set(
                self._namespace + key, entry_to_json(entry), px=_px(ttl, self._default_ttl)
            )
        except redis_sync.RedisError as exc:
            log.warning("redis set failed, entry not cached: %s", exc)

    def delete(self, key: str) -> None:
        """Remove ``key``; a Redis error is logged and ignored."""
        try:
            self._client.unlink(self._namespace + key)
        except redis_sync.RedisError as exc:
            log.warning("redis delete failed: %s", exc)

    def clear(self, prefix: str | None = None) -> None:
        """Remove keys under ``namespace + prefix``; only this store's namespace is touched."""
        pattern = _glob_escape(self._namespace + (prefix or "")) + "*"
        try:
            for batch in _batches(self._client.scan_iter(match=pattern, count=_BATCH)):
                self._client.unlink(*batch)
        except redis_sync.RedisError as exc:
            log.warning("redis clear failed, entries may be stale: %s", exc)

    def clear_many(self, prefixes: Sequence[str]) -> None:
        """Remove keys under any of ``prefixes`` with one ``SCAN`` pass; an outage is a warning."""
        if not prefixes:
            return
        owned = tuple(self._namespace + p for p in prefixes)
        pattern = _scan_pattern(self._namespace, prefixes)
        try:
            matching = (
                k for k in self._client.scan_iter(match=pattern, count=_BATCH) if _under(k, owned)
            )
            for batch in _batches(matching):
                self._client.unlink(*batch)
        except redis_sync.RedisError as exc:
            log.warning("redis clear failed, entries may be stale: %s", exc)


class AsyncRedisStore:
    """`AsyncCacheStore` over ``redis.asyncio.Redis``."""

    def __init__(
        self,
        client: redis_async.Redis,
        *,
        namespace: str = DEFAULT_NAMESPACE,
        default_ttl: float = DEFAULT_TTL,
    ) -> None:
        """Use ``client``; every key is prefixed with ``namespace``."""
        self._client = client
        self._namespace = namespace
        self._default_ttl = default_ttl

    async def get(self, key: str) -> CacheEntry | None:
        """Return the entry; a Redis error or a corrupt value is a miss."""
        try:
            raw = await self._client.get(self._namespace + key)
        except redis_sync.RedisError as exc:
            log.warning("redis get failed, treating as a cache miss: %s", exc)
            return None
        if raw is None:
            return None
        try:
            return entry_from_json(raw)
        except _MISSING_OR_CORRUPT as exc:
            log.warning("corrupt cache value for %s, treating as a miss: %s", key, exc)
            return None

    async def set(self, key: str, entry: CacheEntry, ttl: float | None = None) -> None:
        """Store ``entry``; a Redis error is logged and ignored."""
        try:
            await self._client.set(
                self._namespace + key, entry_to_json(entry), px=_px(ttl, self._default_ttl)
            )
        except redis_sync.RedisError as exc:
            log.warning("redis set failed, entry not cached: %s", exc)

    async def delete(self, key: str) -> None:
        """Remove ``key``; a Redis error is logged and ignored."""
        try:
            await self._client.unlink(self._namespace + key)
        except redis_sync.RedisError as exc:
            log.warning("redis delete failed: %s", exc)

    async def clear(self, prefix: str | None = None) -> None:
        """Remove keys under ``namespace + prefix``; only this store's namespace is touched."""
        await self.clear_many([prefix or ""])

    async def clear_many(self, prefixes: Sequence[str]) -> None:
        """Remove keys under any of ``prefixes`` with one ``SCAN`` pass; an outage is a warning."""
        if not prefixes:
            return
        owned = tuple(self._namespace + p for p in prefixes)
        pattern = _scan_pattern(self._namespace, prefixes)
        try:
            keys = [
                k
                async for k in self._client.scan_iter(match=pattern, count=_BATCH)
                if _under(k, owned)
            ]
            for batch in _batches(keys):
                await self._client.unlink(*batch)
        except redis_sync.RedisError as exc:
            log.warning("redis clear failed, entries may be stale: %s", exc)
