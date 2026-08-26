from __future__ import annotations

import json
import math
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse


@dataclass(frozen=True)
class RateBucket:
    name: str
    capacity: float
    window_sec: float
    cost: float


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _kline_weight(limit: Any) -> int:
    value = int(_number(limit) or 500)
    if value < 100:
        return 1
    if value < 500:
        return 2
    if value <= 1000:
        return 5
    return 10


def binance_request_buckets(
    url: str,
    params: Mapping[str, Any] | None,
    *,
    futures_weight_per_minute: int,
    spot_weight_per_minute: int,
    futures_data_requests_per_5m: int,
) -> tuple[RateBucket, ...]:
    """Map one public Binance request to conservative shared token buckets."""

    parsed = urlparse(str(url or ""))
    host = parsed.hostname or ""
    path = parsed.path
    values = params or {}
    if host == "fapi.binance.com" or host.endswith(".fapi.binance.com"):
        if path.startswith("/futures/data/"):
            return (
                RateBucket(
                    "futures_data_5m",
                    float(max(1, futures_data_requests_per_5m)),
                    300.0,
                    1.0,
                ),
            )
        if path.endswith("/klines"):
            cost = _kline_weight(values.get("limit"))
        elif path.endswith("/ticker/24hr") and not values.get("symbol"):
            cost = 40
        elif path.endswith("/premiumIndex") and not values.get("symbol"):
            cost = 10
        else:
            cost = 1
        return (
            RateBucket(
                "futures_weight_1m",
                float(max(1, futures_weight_per_minute)),
                60.0,
                float(cost),
            ),
        )
    if host == "api.binance.com" or host.endswith(".api.binance.com"):
        if path.endswith("/klines"):
            cost = 2
        elif path.endswith("/exchangeInfo"):
            cost = 20
        elif path.endswith("/ticker/24hr") and not values.get("symbol"):
            cost = 80
        else:
            cost = 1
        return (
            RateBucket(
                "spot_weight_1m",
                float(max(1, spot_weight_per_minute)),
                60.0,
                float(cost),
            ),
        )
    return ()


def is_shareable_public_market_url(url: str) -> bool:
    host = (urlparse(str(url or "")).hostname or "").lower()
    return host in {
        "api.binance.com",
        "fapi.binance.com",
        "www.binance.com",
        "api.coinpaprika.com",
    }


class GlobalBinanceCoordinator:
    """SQLite-backed cache and token buckets shared by local bot processes."""

    def __init__(
        self,
        path: Path,
        *,
        limiter_enabled: bool = True,
        shared_cache_enabled: bool = True,
        futures_weight_per_minute: int = 1200,
        spot_weight_per_minute: int = 3000,
        futures_data_requests_per_5m: int = 800,
        max_wait_sec: float = 6.0,
        cache_max_entries: int = 512,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.path = Path(path)
        self.limiter_enabled = bool(limiter_enabled)
        self.shared_cache_enabled = bool(shared_cache_enabled)
        self.futures_weight_per_minute = max(1, int(futures_weight_per_minute))
        self.spot_weight_per_minute = max(1, int(spot_weight_per_minute))
        self.futures_data_requests_per_5m = max(
            1, int(futures_data_requests_per_5m)
        )
        self.max_wait_sec = max(0.0, float(max_wait_sec))
        self.cache_max_entries = max(16, int(cache_max_entries))
        self.clock = clock
        self.sleeper = sleeper
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=2.0)
        connection.execute("PRAGMA busy_timeout=2000")
        if not self._initialized:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS rate_buckets (
                    bucket TEXT PRIMARY KEY,
                    tokens REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    blocked_until REAL NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    granted INTEGER NOT NULL DEFAULT 0,
                    denied INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS shared_http_cache (
                    cache_key TEXT PRIMARY KEY,
                    stored_at REAL NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_shared_http_cache_stored
                    ON shared_http_cache(stored_at);
                """
            )
            connection.commit()
            self._initialized = True
        return connection

    def _requirements(
        self,
        url: str,
        params: Mapping[str, Any] | None,
    ) -> tuple[RateBucket, ...]:
        return binance_request_buckets(
            url,
            params,
            futures_weight_per_minute=self.futures_weight_per_minute,
            spot_weight_per_minute=self.spot_weight_per_minute,
            futures_data_requests_per_5m=self.futures_data_requests_per_5m,
        )

    def acquire(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        *,
        max_wait_sec: float | None = None,
    ) -> bool:
        requirements = self._requirements(url, params)
        if not self.limiter_enabled or not requirements:
            return True
        wait_budget = self.max_wait_sec if max_wait_sec is None else max(
            0.0, float(max_wait_sec)
        )
        deadline = self.clock() + wait_budget
        while True:
            allowed, wait_sec = self._try_acquire(requirements)
            if allowed:
                return True
            now = self.clock()
            remaining = deadline - now
            if wait_sec <= 0 or wait_sec > remaining:
                return False
            self.sleeper(min(wait_sec, remaining))

    def _try_acquire(
        self,
        requirements: tuple[RateBucket, ...],
    ) -> tuple[bool, float]:
        now = self.clock()
        states: dict[str, dict[str, float | int]] = {}
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            for requirement in requirements:
                row = connection.execute(
                    """
                    SELECT tokens, updated_at, blocked_until, attempts, granted, denied
                    FROM rate_buckets WHERE bucket = ?
                    """,
                    (requirement.name,),
                ).fetchone()
                if row is None:
                    tokens = requirement.capacity
                    updated_at = now
                    blocked_until = 0.0
                    attempts = granted = denied = 0
                else:
                    tokens = float(row[0])
                    updated_at = float(row[1])
                    blocked_until = float(row[2])
                    attempts = int(row[3])
                    granted = int(row[4])
                    denied = int(row[5])
                refill_rate = requirement.capacity / requirement.window_sec
                tokens = min(
                    requirement.capacity,
                    tokens + max(0.0, now - updated_at) * refill_rate,
                )
                states[requirement.name] = {
                    "tokens": tokens,
                    "blocked_until": blocked_until,
                    "attempts": attempts,
                    "granted": granted,
                    "denied": denied,
                }

            wait_sec = 0.0
            blocked_name = ""
            for requirement in requirements:
                state = states[requirement.name]
                blocked_until = float(state["blocked_until"])
                if blocked_until > now:
                    candidate_wait = blocked_until - now
                else:
                    missing = requirement.cost - float(state["tokens"])
                    candidate_wait = (
                        missing / (requirement.capacity / requirement.window_sec)
                        if missing > 0
                        else 0.0
                    )
                if candidate_wait > wait_sec:
                    wait_sec = candidate_wait
                    blocked_name = requirement.name

            allowed = wait_sec <= 0
            for requirement in requirements:
                state = states[requirement.name]
                tokens = float(state["tokens"])
                attempts = int(state["attempts"]) + 1
                granted = int(state["granted"])
                denied = int(state["denied"])
                if allowed:
                    tokens -= requirement.cost
                    granted += 1
                elif requirement.name == blocked_name:
                    denied += 1
                connection.execute(
                    """
                    INSERT INTO rate_buckets(
                        bucket, tokens, updated_at, blocked_until,
                        attempts, granted, denied
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(bucket) DO UPDATE SET
                        tokens=excluded.tokens,
                        updated_at=excluded.updated_at,
                        blocked_until=excluded.blocked_until,
                        attempts=excluded.attempts,
                        granted=excluded.granted,
                        denied=excluded.denied
                    """,
                    (
                        requirement.name,
                        max(0.0, tokens),
                        now,
                        float(state["blocked_until"]),
                        attempts,
                        granted,
                        denied,
                    ),
                )
            connection.commit()
            return allowed, max(0.0, wait_sec)
        except sqlite3.Error:
            if connection is not None:
                connection.rollback()
            return False, 0.0
        finally:
            if connection is not None:
                connection.close()

    def observe_response(
        self,
        url: str,
        params: Mapping[str, Any] | None,
        headers: Mapping[str, Any] | None,
    ) -> None:
        if not self.limiter_enabled or not isinstance(headers, Mapping):
            return
        lowered = {str(key).lower(): value for key, value in headers.items()}
        requirements = self._requirements(url, params)
        for requirement in requirements:
            if requirement.name == "futures_weight_1m":
                used = _number(lowered.get("x-mbx-used-weight-1m"))
            elif requirement.name == "spot_weight_1m":
                used = _number(lowered.get("x-mbx-used-weight-1m"))
            else:
                used = None
            if used is not None:
                self._observe_used(requirement, used)

    def _observe_used(self, requirement: RateBucket, used: float) -> None:
        now = self.clock()
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT tokens, updated_at, blocked_until, attempts, granted, denied "
                "FROM rate_buckets WHERE bucket = ?",
                (requirement.name,),
            ).fetchone()
            if row is None:
                return
            refill = requirement.capacity / requirement.window_sec
            tokens = min(
                requirement.capacity,
                float(row[0]) + max(0.0, now - float(row[1])) * refill,
            )
            observed_available = max(0.0, requirement.capacity - used)
            connection.execute(
                "UPDATE rate_buckets SET tokens = ?, updated_at = ? WHERE bucket = ?",
                (min(tokens, observed_available), now, requirement.name),
            )
            connection.commit()
        except sqlite3.Error:
            if connection is not None:
                connection.rollback()
        finally:
            if connection is not None:
                connection.close()

    def block(
        self,
        url: str,
        params: Mapping[str, Any] | None,
        seconds: float,
    ) -> None:
        if not self.limiter_enabled:
            return
        until = self.clock() + max(1.0, float(seconds))
        requirements = self._requirements(url, params)
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            for requirement in requirements:
                connection.execute(
                    """
                    INSERT INTO rate_buckets(
                        bucket, tokens, updated_at, blocked_until,
                        attempts, granted, denied
                    ) VALUES (?, 0, ?, ?, 0, 0, 0)
                    ON CONFLICT(bucket) DO UPDATE SET
                        tokens=0,
                        updated_at=excluded.updated_at,
                        blocked_until=MAX(rate_buckets.blocked_until, excluded.blocked_until)
                    """,
                    (requirement.name, self.clock(), until),
                )
            connection.commit()
        except sqlite3.Error:
            if connection is not None:
                connection.rollback()
        finally:
            if connection is not None:
                connection.close()

    def cache_get(self, cache_key: str, ttl_sec: int) -> Any | None:
        if not self.shared_cache_enabled or ttl_sec <= 0:
            return None
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            row = connection.execute(
                "SELECT stored_at, payload_json FROM shared_http_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
            if row is None:
                return None
            if self.clock() - float(row[0]) > ttl_sec:
                connection.execute(
                    "DELETE FROM shared_http_cache WHERE cache_key = ?",
                    (cache_key,),
                )
                connection.commit()
                return None
            return json.loads(str(row[1]))
        except (sqlite3.Error, json.JSONDecodeError, TypeError, ValueError):
            return None
        finally:
            if connection is not None:
                connection.close()

    def cache_put(self, cache_key: str, payload: Any) -> None:
        if not self.shared_cache_enabled:
            return
        try:
            serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            return
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO shared_http_cache(cache_key, stored_at, payload_json)
                VALUES (?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    stored_at=excluded.stored_at,
                    payload_json=excluded.payload_json
                """,
                (cache_key, self.clock(), serialized),
            )
            count = int(
                connection.execute("SELECT COUNT(*) FROM shared_http_cache").fetchone()[0]
            )
            overflow = count - self.cache_max_entries
            if overflow > 0:
                connection.execute(
                    """
                    DELETE FROM shared_http_cache WHERE cache_key IN (
                        SELECT cache_key FROM shared_http_cache
                        ORDER BY stored_at ASC LIMIT ?
                    )
                    """,
                    (overflow,),
                )
            connection.commit()
        except sqlite3.Error:
            if connection is not None:
                connection.rollback()
        finally:
            if connection is not None:
                connection.close()

    def diagnostics(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "limiter_enabled": self.limiter_enabled,
            "shared_cache_enabled": self.shared_cache_enabled,
            "buckets": {},
        }
        if not self.path.exists():
            return result
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            rows = connection.execute(
                """
                SELECT bucket, tokens, blocked_until, attempts, granted, denied
                FROM rate_buckets ORDER BY bucket
                """
            ).fetchall()
            now = self.clock()
            result["buckets"] = {
                str(row[0]): {
                    "tokens": round(float(row[1]), 2),
                    "blocked_for_sec": max(0, int(float(row[2]) - now)),
                    "attempts": int(row[3]),
                    "granted": int(row[4]),
                    "denied": int(row[5]),
                }
                for row in rows
            }
            result["shared_cache_entries"] = int(
                connection.execute("SELECT COUNT(*) FROM shared_http_cache").fetchone()[0]
            )
        except sqlite3.Error:
            result["status"] = "unavailable"
        finally:
            if connection is not None:
                connection.close()
        return result


__all__ = [
    "GlobalBinanceCoordinator",
    "RateBucket",
    "binance_request_buckets",
    "is_shareable_public_market_url",
]
