from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional
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

    def consume(self, key: str, amount: int = 1) -> bool:
        limit = self.limits.get(key, 0)
        current = self.used.get(key, 0)
        if limit <= 0:
            return False
        if current + amount > limit:
            return False
        self.used[key] = current + amount
        return True

    def snapshot(self) -> dict[str, dict[str, int]]:
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

    def ok(self, key: str) -> None:
        self.successes[key] = self.successes.get(key, 0) + 1

    def fail(self, key: str, reason: str) -> None:
        self.failures[key] = self.failures.get(key, 0) + 1
        if len(self.warnings) < 12:
            self.warnings.append(f"{key}: {reason}")

    def snapshot(self) -> dict[str, Any]:
        return {
            "successes": self.successes,
            "failures": self.failures,
            "warnings": self.warnings,
            "fused": {key: int(until - time.time()) for key, until in self.fused.items() if until > time.time()},
        }


class HttpClient:
    def __init__(self, settings: Settings, quality: DataQuality):
        self.settings = settings
        self.quality = quality
        self.cache: dict[str, tuple[float, Any]] = {}
        self.fuse_until: dict[str, float] = {}

    def get_json(
        self,
        url: str,
        params: Optional[dict[str, Any]] = None,
        cache_key: Optional[str] = None,
        quality_key: str = "http",
        timeout: Optional[int] = None,
        retries: Optional[int] = None,
    ) -> Any:
        now = time.time()
        fuse_key = quality_key
        if self.fuse_until.get(fuse_key, 0) > now:
            self.quality.fail(quality_key, "fused")
            self.quality.fused[fuse_key] = self.fuse_until[fuse_key]
            return None

        key = cache_key or self._cache_key(url, params)
        if self.settings.http_cache_enable:
            cached = self.cache.get(key)
            if cached and now - cached[0] <= self.settings.http_cache_ttl_sec:
                return cached[1]

        retry_count = self.settings.http_retry if retries is None else retries
        timeout_sec = self.settings.http_timeout_sec if timeout is None else timeout
        last_reason = ""
        for attempt in range(1, retry_count + 1):
            try:
                response = requests.get(url, params=params, headers=HTTP_HEADERS, timeout=timeout_sec)
                if response.status_code == 200:
                    data = response.json()
                    if self.settings.http_cache_enable:
                        self.cache[key] = (time.time(), data)
                    self.quality.ok(quality_key)
                    return data
                last_reason = f"status={response.status_code}"
                if response.status_code in {403, 418, 429}:
                    self.fuse_until[fuse_key] = time.time() + self.settings.fuse_seconds
                    self.quality.fused[fuse_key] = self.fuse_until[fuse_key]
                    break
            except Exception as exc:
                last_reason = type(exc).__name__
            if attempt < retry_count:
                time.sleep(self.settings.http_backoff_sec * attempt)
        self.quality.fail(quality_key, last_reason or "unknown")
        return None

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

    def announcements(self, page_size: int = 50) -> list[dict[str, Any]]:
        url = "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
        articles: list[dict[str, Any]] = []
        for catalog_id in (48, 161, 93):
            for page_no in range(1, 4):
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
        }
