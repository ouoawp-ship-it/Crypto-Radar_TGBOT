from __future__ import annotations

import re
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from math import ceil
from threading import RLock
from typing import Any, Callable, Optional
from urllib.parse import urlencode

import requests

from .config import Settings


HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


@dataclass
class RequestBudget:
    limits: dict[str, int]
    used: dict[str, int] = field(default_factory=dict)
    _lock: RLock = field(default_factory=RLock, repr=False)

    def consume(self, key: str, amount: int = 1) -> bool:
        with self._lock:
            limit = self.limits.get(key, 0)
            current = self.used.get(key, 0)
            if limit <= 0:
                return False
            if current + amount > limit:
                return False
            self.used[key] = current + amount
            return True

    def snapshot(self) -> dict[str, dict[str, int]]:
        with self._lock:
            keys = sorted(set(self.limits) | set(self.used))
            return {
                key: {
                    "used": self.used.get(key, 0),
                    "limit": self.limits.get(key, 0),
                }
                for key in keys
            }


@dataclass
class DataQuality:
    warnings: list[str] = field(default_factory=list)
    failures: dict[str, int] = field(default_factory=dict)
    successes: dict[str, int] = field(default_factory=dict)
    fused: dict[str, float] = field(default_factory=dict)
    _lock: RLock = field(default_factory=RLock, repr=False)

    def ok(self, key: str) -> None:
        with self._lock:
            self.successes[key] = self.successes.get(key, 0) + 1

    def fail(self, key: str, reason: str) -> None:
        with self._lock:
            self.failures[key] = self.failures.get(key, 0) + 1
            if len(self.warnings) < 12:
                self.warnings.append(f"{key}: {reason}")

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "successes": dict(self.successes),
                "failures": dict(self.failures),
                "warnings": list(self.warnings),
                "fused": {key: int(until - time.time()) for key, until in self.fused.items() if until > time.time()},
            }


@dataclass
class _UpstreamSourceStats:
    successes: int = 0
    failures: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    skipped: int = 0
    last_success_at: float | None = None
    last_failure_at: float | None = None
    last_error: str = ""
    durations_ms: deque[float] = field(default_factory=deque)


class UpstreamSourceMetrics:
    """Bounded, process-level health metrics for public upstream data sources."""

    def __init__(
        self,
        *,
        sample_limit: int = 200,
        source_limit: int = 32,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.sample_limit = max(20, int(sample_limit))
        self.source_limit = max(3, int(source_limit))
        self._clock = clock
        self._sources: dict[str, _UpstreamSourceStats] = {}
        self._collapsed_sources = 0
        self._lock = RLock()

    def _stats_for(self, source: str) -> _UpstreamSourceStats:
        normalized = str(source or "unknown").strip().lower() or "unknown"
        stats = self._sources.get(normalized)
        if stats is not None:
            return stats
        # Keep one slot available for unexpected source names so labels stay bounded.
        if len(self._sources) < self.source_limit - 1:
            stats = _UpstreamSourceStats(durations_ms=deque(maxlen=self.sample_limit))
            self._sources[normalized] = stats
            return stats
        self._collapsed_sources += 1
        stats = self._sources.get("other")
        if stats is None:
            stats = _UpstreamSourceStats(durations_ms=deque(maxlen=self.sample_limit))
            self._sources["other"] = stats
        return stats

    def record_cache(self, source: str, *, hit: bool) -> None:
        with self._lock:
            stats = self._stats_for(source)
            if hit:
                stats.cache_hits += 1
            else:
                stats.cache_misses += 1

    def record_network(
        self,
        source: str,
        *,
        success: bool,
        duration_ms: float,
        error: str = "",
    ) -> None:
        with self._lock:
            stats = self._stats_for(source)
            stats.durations_ms.append(max(0.0, float(duration_ms)))
            if success:
                stats.successes += 1
                stats.last_success_at = self._clock()
            else:
                stats.failures += 1
                stats.last_failure_at = self._clock()
                stats.last_error = self._safe_error(error)

    def record_skip(self, source: str, reason: str) -> None:
        with self._lock:
            stats = self._stats_for(source)
            stats.skipped += 1
            stats.last_failure_at = self._clock()
            stats.last_error = self._safe_error(reason or "skipped")

    @staticmethod
    def _safe_error(value: Any) -> str:
        error = str(value or "unknown").strip()[:120]
        if re.fullmatch(r"(?:status=\d{3}|[A-Za-z][A-Za-z0-9_.-]{0,79})", error):
            return error
        return "upstream_error"

    @staticmethod
    def _percentile(values: list[float], quantile: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        index = max(0, ceil(len(ordered) * quantile) - 1)
        return round(ordered[index], 1)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = self._clock()
            sources: dict[str, dict[str, Any]] = {}
            for source, stats in self._sources.items():
                attempts = stats.successes + stats.failures
                cache_total = stats.cache_hits + stats.cache_misses
                latest_failed = (
                    stats.last_failure_at is not None
                    and (stats.last_success_at is None or stats.last_failure_at >= stats.last_success_at)
                )
                if latest_failed:
                    status = "degraded"
                elif stats.successes:
                    status = "ready"
                else:
                    status = "unobserved"
                sources[source] = {
                    "status": status,
                    "attempts": attempts,
                    "successes": stats.successes,
                    "failures": stats.failures,
                    "success_rate": round(stats.successes / attempts, 4) if attempts else None,
                    "p50_ms": self._percentile(list(stats.durations_ms), 0.50),
                    "p95_ms": self._percentile(list(stats.durations_ms), 0.95),
                    "max_ms": round(max(stats.durations_ms), 1) if stats.durations_ms else 0.0,
                    "cache_hits": stats.cache_hits,
                    "cache_misses": stats.cache_misses,
                    "cache_hit_rate": round(stats.cache_hits / cache_total, 4) if cache_total else None,
                    "skipped": stats.skipped,
                    "data_age_sec": (
                        max(0, int(now - stats.last_success_at))
                        if stats.last_success_at is not None
                        else None
                    ),
                    "last_error": stats.last_error,
                }
            statuses = {item["status"] for item in sources.values()}
            if "degraded" in statuses:
                status = "degraded"
            elif "ready" in statuses:
                status = "ready"
            else:
                status = "unobserved"
            return {
                "scope": "process",
                "status": status,
                "source_limit": self.source_limit,
                "collapsed_sources": self._collapsed_sources,
                "sources": sources,
            }


UPSTREAM_SOURCE_METRICS = UpstreamSourceMetrics()


def _source_id_from_quality_key(quality_key: str) -> str:
    key = str(quality_key or "http").strip()
    if key.startswith("funding:"):
        exchange = key.split(":", 1)[1].strip().lower()
        return "binance_futures_public" if exchange == "binance" else f"{exchange}_funding_public"
    if key == "spotKlines":
        return "binance_spot_public"
    if key == "announcements":
        return "binance_announcements"
    if key == "coinpaprikaMarketCaps":
        return "coinpaprika_market"
    if key == "marketCaps":
        return "binance_market_metadata"
    if key in {"exchangeInfo", "ticker24hr", "premiumIndex", "openInterestHist", "klines", "fundingRate", "depth"}:
        return "binance_futures_public"
    return "other"


class HttpClient:
    def __init__(
        self,
        settings: Settings,
        quality: DataQuality,
        session: requests.Session | None = None,
        *,
        metrics: UpstreamSourceMetrics | None = None,
        cache_max_entries: int | None = None,
    ):
        self.settings = settings
        self.quality = quality
        self._owns_session = session is None
        self._closed = False
        self.session = session if session is not None else requests.Session()
        self.metrics = metrics or UPSTREAM_SOURCE_METRICS
        configured_cache_limit = (
            getattr(settings, "http_cache_max_entries", 128)
            if cache_max_entries is None
            else cache_max_entries
        )
        self.cache_max_entries = max(1, int(configured_cache_limit))
        self.cache: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._cache_evictions = 0
        self._cache_expired_pruned = 0
        self.fuse_until: dict[str, float] = {}
        self._state_lock = RLock()

    def close(self) -> None:
        with self._state_lock:
            self.cache.clear()
            self.fuse_until.clear()
        if self._owns_session and not self._closed:
            self.session.close()
            self._closed = True

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def get_json(
        self,
        url: str,
        params: Optional[dict[str, Any]] = None,
        cache_key: Optional[str] = None,
        quality_key: str = "http",
        timeout: Optional[int] = None,
        retries: Optional[int] = None,
        cache: bool = True,
    ) -> Any:
        now = time.time()
        fuse_key = quality_key
        source_id = _source_id_from_quality_key(quality_key)
        with self._state_lock:
            if self.fuse_until.get(fuse_key, 0) > now:
                self.quality.fail(quality_key, "fused")
                self.quality.fused[fuse_key] = self.fuse_until[fuse_key]
                self.metrics.record_skip(source_id, "fused")
                return None

        key = cache_key or self._cache_key(url, params)
        use_cache = bool(self.settings.http_cache_enable and cache)
        if use_cache:
            with self._state_lock:
                self._prune_cache_locked(now)
                cached = self.cache.get(key)
                if cached is not None:
                    self.cache.move_to_end(key)
            self.metrics.record_cache(source_id, hit=cached is not None)
            if cached is not None:
                return cached[1]

        retry_count = self.settings.http_retry if retries is None else retries
        timeout_sec = self.settings.http_timeout_sec if timeout is None else timeout
        last_reason = ""
        started_at = time.perf_counter()
        for attempt in range(1, retry_count + 1):
            try:
                response = self.session.get(url, params=params, headers=HTTP_HEADERS, timeout=timeout_sec)
                if response.status_code == 200:
                    data = response.json()
                    with self._state_lock:
                        if use_cache:
                            self._prune_cache_locked(time.time())
                            self.cache.pop(key, None)
                            while len(self.cache) >= self.cache_max_entries:
                                self.cache.popitem(last=False)
                                self._cache_evictions += 1
                            self.cache[key] = (time.time(), data)
                        self.quality.ok(quality_key)
                    self.metrics.record_network(
                        source_id,
                        success=True,
                        duration_ms=(time.perf_counter() - started_at) * 1000,
                    )
                    return data
                last_reason = f"status={response.status_code}"
                if response.status_code in {403, 418, 429}:
                    with self._state_lock:
                        self.fuse_until[fuse_key] = time.time() + self.settings.fuse_seconds
                        self.quality.fused[fuse_key] = self.fuse_until[fuse_key]
                    break
            except Exception as exc:
                last_reason = type(exc).__name__
            if attempt < retry_count:
                time.sleep(self.settings.http_backoff_sec * attempt)
        with self._state_lock:
            self.quality.fail(quality_key, last_reason or "unknown")
        self.metrics.record_network(
            source_id,
            success=False,
            duration_ms=(time.perf_counter() - started_at) * 1000,
            error=last_reason or "unknown",
        )
        return None

    def _prune_cache_locked(self, now: float) -> None:
        ttl = max(0, int(self.settings.http_cache_ttl_sec))
        expired = [key for key, (stored_at, _) in self.cache.items() if now - stored_at > ttl]
        for key in expired:
            self.cache.pop(key, None)
        self._cache_expired_pruned += len(expired)

    def diagnostics(self) -> dict[str, int]:
        with self._state_lock:
            if self.settings.http_cache_enable:
                self._prune_cache_locked(time.time())
            return {
                "entries": len(self.cache),
                "max_entries": self.cache_max_entries,
                "evictions": self._cache_evictions,
                "expired_pruned": self._cache_expired_pruned,
            }

    @staticmethod
    def _cache_key(url: str, params: Optional[dict[str, Any]]) -> str:
        if not params:
            return url
        return f"{url}?{urlencode(sorted(params.items()))}"


class BinanceDataSource:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.quality = DataQuality()
        self.budget = RequestBudget({
            "open_interest_hist": settings.oi_hist_budget,
            "klines": settings.kline_budget,
            "spot_klines": settings.kline_budget,
            "funding_history": settings.funding_history_budget,
        })
        self.http = HttpClient(settings, self.quality)

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> BinanceDataSource:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def endpoint(self, path: str) -> str:
        return f"{self.settings.binance_fapi_base_url}{path}"

    def spot_endpoint(self, path: str) -> str:
        return f"{self.settings.binance_spot_base_url}{path}"

    def exchange_info(self) -> dict[str, Any] | None:
        return self.http.get_json(
            self.endpoint("/fapi/v1/exchangeInfo"),
            cache_key="fapi:exchangeInfo",
            quality_key="exchangeInfo",
        )

    def usdt_perp_symbols(self) -> list[dict[str, Any]]:
        info = self.exchange_info()
        if not info:
            return []
        return [
            item for item in info.get("symbols", [])
            if item.get("contractType") == "PERPETUAL"
            and item.get("quoteAsset") == "USDT"
            and item.get("status") == "TRADING"
        ]

    def ticker_24h(self) -> list[dict[str, Any]]:
        data = self.http.get_json(
            self.endpoint("/fapi/v1/ticker/24hr"),
            cache_key="fapi:ticker24hr",
            quality_key="ticker24hr",
        )
        return data if isinstance(data, list) else []

    def premium_index(self) -> list[dict[str, Any]]:
        data = self.http.get_json(
            self.endpoint("/fapi/v1/premiumIndex"),
            cache_key="fapi:premiumIndex",
            quality_key="premiumIndex",
        )
        return data if isinstance(data, list) else []

    def open_interest_hist(
        self,
        symbol: str,
        period: str = "1h",
        limit: int = 6,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[dict[str, Any]]:
        if not self.budget.consume("open_interest_hist"):
            self.quality.fail("openInterestHist", "budget_exhausted")
            return []
        params: dict[str, Any] = {"symbol": symbol, "period": period, "limit": limit}
        if start_time is not None:
            params["startTime"] = int(start_time)
        if end_time is not None:
            params["endTime"] = int(end_time)
        data = self.http.get_json(
            self.endpoint("/futures/data/openInterestHist"),
            params,
            cache_key=f"oi:{symbol}:{period}:{limit}:{params.get('startTime', '')}:{params.get('endTime', '')}",
            quality_key="openInterestHist",
            retries=1,
            cache=False,
        )
        return data if isinstance(data, list) else []

    def klines(
        self,
        symbol: str,
        interval: str = "1d",
        limit: int = 120,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[list[Any]]:
        if not self.budget.consume("klines"):
            self.quality.fail("klines", "budget_exhausted")
            return []
        params: dict[str, Any] = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_time is not None:
            params["startTime"] = int(start_time)
        if end_time is not None:
            params["endTime"] = int(end_time)
        data = self.http.get_json(
            self.endpoint("/fapi/v1/klines"),
            params,
            cache_key=f"klines:{symbol}:{interval}:{limit}:{params.get('startTime', '')}:{params.get('endTime', '')}",
            quality_key="klines",
            retries=1,
            cache=False,
        )
        return data if isinstance(data, list) else []

    def spot_klines(
        self,
        symbol: str,
        interval: str = "1d",
        limit: int = 120,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[list[Any]]:
        if not self.budget.consume("spot_klines"):
            self.quality.fail("spotKlines", "budget_exhausted")
            return []
        params: dict[str, Any] = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_time is not None:
            params["startTime"] = int(start_time)
        if end_time is not None:
            params["endTime"] = int(end_time)
        data = self.http.get_json(
            self.spot_endpoint("/api/v3/klines"),
            params,
            cache_key=f"spot:klines:{symbol}:{interval}:{limit}:{params.get('startTime', '')}:{params.get('endTime', '')}",
            quality_key="spotKlines",
            retries=1,
            cache=False,
        )
        return data if isinstance(data, list) else []

    def funding_rate(self, symbol: str, limit: int = 5) -> list[dict[str, Any]]:
        if not self.budget.consume("funding_history"):
            self.quality.fail("fundingRate", "budget_exhausted")
            return []
        data = self.http.get_json(
            self.endpoint("/fapi/v1/fundingRate"),
            {"symbol": symbol, "limit": limit},
            cache_key=f"funding:{symbol}:{limit}",
            quality_key="fundingRate",
            retries=1,
            cache=False,
        )
        return data if isinstance(data, list) else []

    def order_book(self, symbol: str, limit: int = 100) -> dict[str, Any]:
        safe_limit = min(1000, max(5, int(limit or 100)))
        data = self.http.get_json(
            self.endpoint("/fapi/v1/depth"),
            {"symbol": symbol.upper(), "limit": safe_limit},
            cache_key=f"depth:{symbol.upper()}:{safe_limit}",
            quality_key="depth",
            retries=1,
            cache=False,
        )
        return data if isinstance(data, dict) else {}

    def market_caps(self) -> dict[str, float]:
        url = "https://www.binance.com/bapi/composite/v1/public/marketing/symbol/list"
        data = self.http.get_json(url, cache_key="binance:marketing-symbol-list", quality_key="marketCaps")
        result: dict[str, float] = {}
        if not isinstance(data, dict):
            return result
        raw_items = data.get("data") or data.get("symbols") or []
        if isinstance(raw_items, dict):
            raw_items = raw_items.get("list") or raw_items.get("symbols") or []
        for item in raw_items if isinstance(raw_items, list) else []:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or item.get("baseAsset") or "").upper().replace("USDT", "")
            value = item.get("marketCap") or item.get("marketCapUsd") or item.get("circulatingMarketCap")
            try:
                cap = float(value)
            except (TypeError, ValueError):
                continue
            if symbol and cap > 0:
                result[symbol] = cap
        return result

    def coinpaprika_market_caps(self) -> dict[str, float]:
        data = self.http.get_json(
            "https://api.coinpaprika.com/v1/tickers",
            {"quotes": "USD"},
            cache_key="coinpaprika:tickers:usd",
            quality_key="coinpaprikaMarketCaps",
            timeout=15,
            retries=1,
            cache=False,
        )
        result: dict[str, tuple[float, int]] = {}
        if not isinstance(data, list):
            return {}
        for item in data:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "").upper().strip()
            quotes = item.get("quotes") if isinstance(item.get("quotes"), dict) else {}
            usd_quote = quotes.get("USD") if isinstance(quotes.get("USD"), dict) else {}
            value = (
                usd_quote.get("market_cap")
                or usd_quote.get("market_cap_usd")
                or item.get("market_cap")
                or item.get("market_cap_usd")
            )
            try:
                cap = float(value)
            except (TypeError, ValueError):
                continue
            if not symbol or cap <= 0:
                continue
            try:
                rank = int(item.get("rank") or 999_999)
            except (TypeError, ValueError):
                rank = 999_999
            current = result.get(symbol)
            if current is None or rank < current[1]:
                result[symbol] = (cap, rank)
        return {symbol: cap for symbol, (cap, _rank) in result.items()}

    def announcements(self, page_size: int = 50, max_pages: int = 3) -> list[dict[str, Any]]:
        url = "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
        articles: list[dict[str, Any]] = []
        safe_pages = max(1, min(3, int(max_pages or 1)))
        for catalog_id in (48, 161, 93):
            for page_no in range(1, safe_pages + 1):
                data = self.http.get_json(
                    url,
                    {"type": 1, "catalogId": catalog_id, "pageNo": page_no, "pageSize": page_size},
                    cache_key=f"binance:announcements:{catalog_id}:{page_size}:{page_no}",
                    quality_key="announcements",
                )
                if not isinstance(data, dict):
                    continue
                catalogs = data.get("data", {}).get("catalogs", [])
                if not isinstance(catalogs, list):
                    continue
                page_articles = 0
                for catalog in catalogs:
                    for article in catalog.get("articles", []) if isinstance(catalog, dict) else []:
                        if isinstance(article, dict):
                            article["_catalog_id"] = catalog_id
                            article["_page_no"] = page_no
                            articles.append(article)
                            page_articles += 1
                if page_articles <= 0:
                    break

        unique: list[dict[str, Any]] = []
        seen: set[str] = set()
        for article in articles:
            code = str(article.get("code") or article.get("id") or article.get("title") or "")
            if not code or code in seen:
                continue
            seen.add(code)
            unique.append(article)
        return unique

    def diagnostics(self) -> dict[str, Any]:
        return {
            "budget": self.budget.snapshot(),
            "quality": self.quality.snapshot(),
            "http": self.http.diagnostics(),
        }
