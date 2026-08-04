from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar


T = TypeVar("T")
DEFAULT_MAX_ENTRIES = 512


@dataclass(frozen=True)
class _CacheEntry:
    value: Any
    expires_at: float


class RuntimeCache:
    """Small in-process TTL cache with per-key single-flight loading."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        self._clock = clock
        self.max_entries = max(1, int(max_entries))
        self._condition = threading.Condition(threading.RLock())
        self._entries: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._loading: set[str] = set()
        self._generation = 0
        self._hits = 0
        self._misses = 0
        self._loads = 0
        self._load_errors = 0
        self._waits = 0
        self._invalidations = 0
        self._evictions = 0
        self._expired_pruned = 0

    def _prune_expired_locked(self, now: float) -> None:
        expired = [key for key, entry in self._entries.items() if entry.expires_at <= now]
        for key in expired:
            self._entries.pop(key, None)
        self._expired_pruned += len(expired)

    def get_or_set(self, key: str, ttl_sec: float, loader: Callable[[], T]) -> T:
        cache_key = str(key)
        ttl = max(0.0, float(ttl_sec))

        with self._condition:
            while True:
                now = self._clock()
                self._prune_expired_locked(now)
                entry = self._entries.get(cache_key)
                if entry is not None:
                    self._entries.move_to_end(cache_key)
                    self._hits += 1
                    return entry.value

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
                self._prune_expired_locked(self._clock())
                self._entries.pop(cache_key, None)
                while len(self._entries) >= self.max_entries:
                    self._entries.popitem(last=False)
                    self._evictions += 1
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
            self._prune_expired_locked(self._clock())
            return {
                "entries": len(self._entries),
                "max_entries": self.max_entries,
                "loading": len(self._loading),
                "hits": self._hits,
                "misses": self._misses,
                "loads": self._loads,
                "load_errors": self._load_errors,
                "waits": self._waits,
                "invalidations": self._invalidations,
                "evictions": self._evictions,
                "expired_pruned": self._expired_pruned,
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
