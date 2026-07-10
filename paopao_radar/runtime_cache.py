from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar


T = TypeVar("T")


@dataclass(frozen=True)
class _CacheEntry:
    value: Any
    expires_at: float


class RuntimeCache:
    """Small in-process TTL cache with per-key single-flight loading."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._condition = threading.Condition(threading.RLock())
        self._entries: dict[str, _CacheEntry] = {}
        self._loading: set[str] = set()
        self._generation = 0
        self._hits = 0
        self._misses = 0
        self._loads = 0
        self._load_errors = 0
        self._waits = 0
        self._invalidations = 0

    def get_or_set(self, key: str, ttl_sec: float, loader: Callable[[], T]) -> T:
        cache_key = str(key)
        ttl = max(0.0, float(ttl_sec))

        with self._condition:
            while True:
                now = self._clock()
                entry = self._entries.get(cache_key)
                if entry is not None and entry.expires_at > now:
                    self._hits += 1
                    return entry.value
                if entry is not None:
                    self._entries.pop(cache_key, None)

                if cache_key not in self._loading:
                    self._loading.add(cache_key)
                    self._misses += 1
                    generation = self._generation
                    break

                self._waits += 1
                self._condition.wait()

        try:
            value = loader()
        except BaseException:
            with self._condition:
                self._load_errors += 1
                self._loading.discard(cache_key)
                self._condition.notify_all()
            raise

        with self._condition:
            self._loads += 1
            if ttl > 0 and generation == self._generation:
                self._entries[cache_key] = _CacheEntry(value=value, expires_at=self._clock() + ttl)
            self._loading.discard(cache_key)
            self._condition.notify_all()
        return value

    def invalidate(self, prefix: str | None = None) -> int:
        with self._condition:
            self._generation += 1
            if prefix is None:
                removed = len(self._entries)
                self._entries.clear()
            else:
                cache_prefix = str(prefix)
                keys = [key for key in self._entries if key.startswith(cache_prefix)]
                for key in keys:
                    self._entries.pop(key, None)
                removed = len(keys)
            self._invalidations += 1
            return removed

    def clear(self) -> int:
        return self.invalidate()

    def stats(self) -> dict[str, int]:
        with self._condition:
            now = self._clock()
            expired = [key for key, entry in self._entries.items() if entry.expires_at <= now]
            for key in expired:
                self._entries.pop(key, None)
            return {
                "entries": len(self._entries),
                "loading": len(self._loading),
                "hits": self._hits,
                "misses": self._misses,
                "loads": self._loads,
                "load_errors": self._load_errors,
                "waits": self._waits,
                "invalidations": self._invalidations,
            }


_CACHE = RuntimeCache()


def get_or_set(key: str, ttl_sec: float, loader: Callable[[], T]) -> T:
    return _CACHE.get_or_set(key, ttl_sec, loader)


def invalidate(prefix: str | None = None) -> int:
    return _CACHE.invalidate(prefix)


def clear() -> int:
    return _CACHE.clear()


def stats() -> dict[str, int]:
    return _CACHE.stats()
