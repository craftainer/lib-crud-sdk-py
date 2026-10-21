"""In-memory cache stores: LRU with per-entry TTL."""

import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence

from craftainer_crud_sdk.interfaces import CacheEntry

__all__ = ["AsyncMemoryStore", "MemoryStore"]

DEFAULT_TTL = 3600.0
"""Seconds the store keeps an entry when the caller gives no ttl (the spec's retention floor)."""


class MemoryStore:
    """Thread-safe LRU store (default 1024 entries) that also honours each entry's ttl."""

    def __init__(
        self,
        max_entries: int = 1024,
        *,
        default_ttl: float = DEFAULT_TTL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Keep at most ``max_entries``; ``clock`` is injectable for tests."""
        if max_entries < 1:
            msg = "max_entries must be at least 1"
            raise ValueError(msg)
        self._max_entries = max_entries
        self._default_ttl = default_ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._data: OrderedDict[str, tuple[float, CacheEntry]] = OrderedDict()

    def __len__(self) -> int:
        """Number of stored entries (expired ones included until touched)."""
        with self._lock:
            return len(self._data)

    def get(self, key: str) -> CacheEntry | None:
        """Return the entry and mark it recently used; expired entries are dropped."""
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            expires_at, entry = item
            if expires_at <= self._clock():
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return entry

    def set(self, key: str, entry: CacheEntry, ttl: float | None = None) -> None:
        """Store ``entry``; evict the least recently used entries beyond the capacity."""
        expires_at = self._clock() + (self._default_ttl if ttl is None else ttl)
        with self._lock:
            self._data[key] = (expires_at, entry)
            self._data.move_to_end(key)
            while len(self._data) > self._max_entries:
                self._data.popitem(last=False)

    def delete(self, key: str) -> None:
        """Remove ``key`` if present."""
        with self._lock:
            self._data.pop(key, None)

    def clear(self, prefix: str | None = None) -> None:
        """Remove every key starting with ``prefix``, or everything when it is ``None``."""
        with self._lock:
            if prefix is None:
                self._data.clear()
                return
            for key in [k for k in self._data if k.startswith(prefix)]:
                del self._data[key]

    def clear_many(self, prefixes: Sequence[str]) -> None:
        """Remove every key starting with any of ``prefixes``, in one pass."""
        if not prefixes:
            return
        wanted = tuple(prefixes)
        with self._lock:
            for key in [k for k in self._data if k.startswith(wanted)]:
                del self._data[key]


class AsyncMemoryStore:
    """`AsyncCacheStore` over a `MemoryStore`; nothing blocks, so the methods just delegate."""

    def __init__(self, store: MemoryStore | None = None) -> None:
        """Wrap ``store`` (a fresh default `MemoryStore` if omitted)."""
        self._store = MemoryStore() if store is None else store

    async def get(self, key: str) -> CacheEntry | None:
        """Return the entry, or ``None`` if absent or expired."""
        return self._store.get(key)

    async def set(self, key: str, entry: CacheEntry, ttl: float | None = None) -> None:
        """Store ``entry`` under ``key``."""
        self._store.set(key, entry, ttl)

    async def delete(self, key: str) -> None:
        """Remove ``key`` if present."""
        self._store.delete(key)

    async def clear(self, prefix: str | None = None) -> None:
        """Remove every key starting with ``prefix``, or everything."""
        self._store.clear(prefix)

    async def clear_many(self, prefixes: Sequence[str]) -> None:
        """Remove every key starting with any of ``prefixes``, in one pass."""
        self._store.clear_many(prefixes)
