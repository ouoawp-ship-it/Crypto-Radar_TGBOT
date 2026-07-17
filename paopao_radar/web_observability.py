from __future__ import annotations

import threading
import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from typing import Any


ALLOWED_TELEMETRY_EVENTS = {
    "frontend_api_error",
    "frontend_render_error",
    "frontend_unhandled_error",
    "frontend_route_loaded",
}


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    reset_after_sec: int


class SlidingWindowRateLimiter:
    def __init__(self, *, window_sec: int = 60, max_keys: int = 10_000, clock: Any = time.monotonic) -> None:
        self.window_sec = max(1, int(window_sec))
        self.max_keys = max(100, int(max_keys))
        self._clock = clock
        self._lock = threading.RLock()
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._allowed = 0
        self._blocked = 0

    def allow(self, key: str, limit: int) -> RateLimitResult:
        safe_limit = max(1, int(limit))
        now = float(self._clock())
        cutoff = now - self.window_sec
        safe_key = str(key)
        with self._lock:
            if safe_key not in self._events and len(self._events) >= self.max_keys:
                expired_keys = [
                    existing_key
                    for existing_key, existing_events in self._events.items()
                    if not existing_events or existing_events[-1] <= cutoff
                ]
                for existing_key in expired_keys:
                    self._events.pop(existing_key, None)
                if len(self._events) >= self.max_keys:
                    oldest_key = min(
                        self._events,
                        key=lambda existing_key: self._events[existing_key][-1] if self._events[existing_key] else float("-inf"),
                    )
                    self._events.pop(oldest_key, None)
            events = self._events[safe_key]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= safe_limit:
                self._blocked += 1
                reset = max(1, int(math_ceil(events[0] + self.window_sec - now)))
                return RateLimitResult(False, safe_limit, 0, reset)
            events.append(now)
            self._allowed += 1
            remaining = max(0, safe_limit - len(events))
            return RateLimitResult(True, safe_limit, remaining, self.window_sec)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "active_keys": len(self._events),
                "allowed": self._allowed,
                "blocked": self._blocked,
                "window_sec": self.window_sec,
                "max_keys": self.max_keys,
            }


def math_ceil(value: float) -> int:
    number = int(value)
    return number if number == value else number + 1


class PublicTelemetry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._counts: Counter[str] = Counter()
        self._last_at: dict[str, int] = {}

    def record(self, event: str) -> bool:
        event_name = str(event or "").strip().lower()
        if event_name not in ALLOWED_TELEMETRY_EVENTS:
            return False
        with self._lock:
            self._counts[event_name] += 1
            self._last_at[event_name] = int(time.time())
        return True

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {"counts": dict(self._counts), "last_at": dict(self._last_at)}


class PublicApiMetrics:
    def __init__(self, sample_limit: int = 500) -> None:
        self.sample_limit = max(20, int(sample_limit))
        self._lock = threading.RLock()
        self._durations: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=self.sample_limit))
        self._status: Counter[str] = Counter()

    def record(self, path: str, status: int, duration_ms: float) -> None:
        safe_path = str(path or "unknown")[:80]
        with self._lock:
            self._durations[safe_path].append(max(0.0, float(duration_ms)))
            self._status[f"{int(status) // 100}xx"] += 1

    def stats(self) -> dict[str, Any]:
        with self._lock:
            routes: dict[str, Any] = {}
            for path, values in self._durations.items():
                ordered = sorted(values)
                if not ordered:
                    continue
                index = min(len(ordered) - 1, max(0, math_ceil(len(ordered) * 0.95) - 1))
                routes[path] = {
                    "count": len(ordered),
                    "p95_ms": round(ordered[index], 1),
                    "max_ms": round(max(ordered), 1),
                }
            return {"routes": routes, "status_classes": dict(self._status)}


class PublicStreamMetrics:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active = 0
        self._opened = 0
        self._closed = 0
        self._errors = 0
        self._events: Counter[str] = Counter()

    def opened(self) -> None:
        with self._lock:
            self._active += 1
            self._opened += 1

    def event(self, event_type: str) -> None:
        with self._lock:
            self._events[str(event_type or "unknown")[:40]] += 1

    def closed(self, *, error: bool = False) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)
            self._closed += 1
            if error:
                self._errors += 1

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "active": self._active,
                "opened": self._opened,
                "closed": self._closed,
                "errors": self._errors,
                "events": dict(self._events),
            }


PUBLIC_API_LIMITER = SlidingWindowRateLimiter(window_sec=60)
PUBLIC_TELEMETRY = PublicTelemetry()
PUBLIC_API_METRICS = PublicApiMetrics()
PUBLIC_STREAM_METRICS = PublicStreamMetrics()
