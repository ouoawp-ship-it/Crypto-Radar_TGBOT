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
            "funding_history": settings.funding_history_budget,
        })
        self.http = HttpClient(settings, self.quality)

    def endpoint(self, path: str) -> str:
        return f"{self.settings.binance_fapi_base_url}{path}"

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

    def announcements(self, page_size: int = 20) -> list[dict[str, Any]]:
        url = "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
        articles: list[dict[str, Any]] = []
        for catalog_id in (48, 161, 93):
            data = self.http.get_json(
                url,
                {"type": 1, "catalogId": catalog_id, "pageNo": 1, "pageSize": page_size},
                cache_key=f"binance:announcements:{catalog_id}:{page_size}",
                quality_key="announcements",
            )
            if not isinstance(data, dict):
                continue
            catalogs = data.get("data", {}).get("catalogs", [])
            if not isinstance(catalogs, list):
                continue
            for catalog in catalogs:
                for article in catalog.get("articles", []) if isinstance(catalog, dict) else []:
                    if isinstance(article, dict):
                        article["_catalog_id"] = catalog_id
                        articles.append(article)

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


class CoinglassDataSource:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.quality = DataQuality()
        self.budget = RequestBudget({"coinglass": settings.coinglass_request_budget})
        self.http = HttpClient(settings, self.quality)

    @property
    def enabled(self) -> bool:
        return bool(self.settings.coinglass_enable and self.settings.coinglass_api_key)

    def endpoint(self, path: str) -> str:
        return f"{self.settings.coinglass_base_url}{path}"

    def get_json(
        self,
        path: str,
        params: Optional[dict[str, Any]] = None,
        quality_key: str = "coinglass",
        timeout_sec: int | None = None,
    ) -> Any:
        if not self.enabled:
            self.quality.fail(quality_key, "disabled_or_missing_api_key")
            return None
        if not self.budget.consume("coinglass"):
            self.quality.fail(quality_key, "budget_exhausted")
            return None
        return self._request_json(path, params, quality_key, timeout_sec=timeout_sec)

    def _request_json(
        self,
        path: str,
        params: Optional[dict[str, Any]],
        quality_key: str,
        timeout_sec: int | None = None,
    ) -> Any:
        url = self.endpoint(path)
        cache_key = f"coinglass:{path}:{urlencode(sorted((params or {}).items()))}"
        if self.settings.http_cache_enable:
            cached = self.http.cache.get(cache_key)
            if cached and time.time() - cached[0] <= self.settings.http_cache_ttl_sec:
                return cached[1]
        headers = {
            **HTTP_HEADERS,
            "CG-API-KEY": self.settings.coinglass_api_key,
            "accept": "application/json",
        }
        last_reason = ""
        for attempt in range(1, self.settings.http_retry + 1):
            try:
                response = requests.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=timeout_sec or self.settings.coinglass_timeout_sec,
                )
                if response.status_code == 200:
                    data = response.json()
                    if self.settings.http_cache_enable:
                        self.http.cache[cache_key] = (time.time(), data)
                    self.quality.ok(quality_key)
                    return data
                last_reason = f"status={response.status_code}"
                if response.status_code in {401, 403, 418, 429}:
                    break
            except Exception as exc:
                last_reason = type(exc).__name__
            if attempt < self.settings.http_retry:
                time.sleep(self.settings.http_backoff_sec * attempt)
        self.quality.fail(quality_key, last_reason or "unknown")
        return None

    @staticmethod
    def unwrap_data(payload: Any) -> Any:
        if isinstance(payload, dict):
            for key in ("data", "result"):
                if key in payload:
                    return payload[key]
        return payload

    def open_interest_exchange_list(self, symbol: str) -> Any:
        payload = self.get_json(
            "/api/futures/open-interest/exchange-list",
            {"symbol": symbol.upper().replace("USDT", "")},
            quality_key="coinglassOpenInterestExchangeList",
        )
        return self.unwrap_data(payload)

    def liquidation_heatmap(
        self,
        exchange: str,
        symbol: str,
        range_: str = "3d",
    ) -> Any:
        payload = self.get_json(
            "/api/futures/liquidation/heatmap/model1",
            {
                "exchange": exchange,
                "symbol": symbol.upper(),
                "range": range_,
            },
            quality_key="coinglassLiquidationHeatmap",
            timeout_sec=self.settings.coinglass_liquidity_timeout_sec,
        )
        if payload is None:
            payload = self.get_json(
                "/api/futures/liquidation/aggregated-heatmap/model1",
                {
                    "symbol": symbol.upper().replace("USDT", ""),
                    "range": range_,
                },
                quality_key="coinglassLiquidationAggregatedHeatmap",
                timeout_sec=self.settings.coinglass_liquidity_timeout_sec,
            )
        return self.unwrap_data(payload)

    def orderbook_heatmap(
        self,
        exchange: str,
        symbol: str,
        range_: str = "24h",
    ) -> Any:
        interval = "1h"
        if range_ in {"1m", "3m", "5m", "15m", "30m", "1h", "4h", "6h", "8h", "12h", "1d"}:
            interval = range_
        payload = self.get_json(
            "/api/futures/orderbook/history",
            {
                "exchange": exchange,
                "symbol": symbol.upper(),
                "interval": interval,
                "limit": 100,
            },
            quality_key="coinglassOrderbookHeatmap",
            timeout_sec=self.settings.coinglass_liquidity_timeout_sec,
        )
        return self.unwrap_data(payload)

    def open_interest_history(
        self,
        exchange: str,
        symbol: str,
        interval: str = "1d",
        limit: int = 10,
        unit: str = "usd",
    ) -> Any:
        payload = self.get_json(
            "/api/futures/open-interest/history",
            {
                "exchange": exchange,
                "symbol": symbol.upper(),
                "interval": interval,
                "limit": limit,
                "unit": unit,
            },
            quality_key="coinglassOpenInterestHistory",
        )
        return self.unwrap_data(payload)

    def liquidation_exchange_list(self, symbol: str = "", range_: str = "1h") -> Any:
        params = {"range": range_}
        if symbol:
            params["symbol"] = symbol.upper().replace("USDT", "")
        payload = self.get_json(
            "/api/futures/liquidation/exchange-list",
            params,
            quality_key="coinglassLiquidationExchangeList",
        )
        return self.unwrap_data(payload)

    def coins_markets(
        self,
        exchange_list: str = "",
        per_page: int = 100,
        page: int = 1,
    ) -> Any:
        params: dict[str, Any] = {
            "per_page": max(1, int(per_page)),
            "page": max(1, int(page)),
        }
        if exchange_list:
            params["exchange_list"] = exchange_list
        payload = self.get_json(
            "/api/futures/coins-markets",
            params,
            quality_key="coinglassCoinsMarkets",
        )
        return self.unwrap_data(payload)

    def futures_aggregated_cvd_history(
        self,
        symbol: str,
        exchange_list: str = "Binance",
        interval: str = "1h",
        limit: int = 6,
        unit: str = "usd",
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> Any:
        params: dict[str, Any] = {
            "exchange_list": exchange_list,
            "symbol": symbol.upper().replace("USDT", ""),
            "interval": interval,
            "limit": limit,
            "unit": unit,
        }
        if start_time is not None:
            params["start_time"] = int(start_time)
        if end_time is not None:
            params["end_time"] = int(end_time)
        payload = self.get_json(
            "/api/futures/aggregated-cvd/history",
            params,
            quality_key="coinglassFuturesAggregatedCvd",
        )
        return self.unwrap_data(payload)

    def spot_aggregated_cvd_history(
        self,
        symbol: str,
        exchange_list: str = "Binance",
        interval: str = "1h",
        limit: int = 6,
        unit: str = "usd",
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> Any:
        params: dict[str, Any] = {
            "exchange_list": exchange_list,
            "symbol": symbol.upper().replace("USDT", ""),
            "interval": interval,
            "limit": limit,
            "unit": unit,
        }
        if start_time is not None:
            params["start_time"] = int(start_time)
        if end_time is not None:
            params["end_time"] = int(end_time)
        payload = self.get_json(
            "/api/spot/aggregated-cvd/history",
            params,
            quality_key="coinglassSpotAggregatedCvd",
        )
        return self.unwrap_data(payload)

    def funding_rate_history(
        self,
        exchange: str,
        symbol: str,
        interval: str = "1h",
        limit: int = 6,
    ) -> Any:
        payload = self.get_json(
            "/api/futures/funding-rate/history",
            {
                "exchange": exchange,
                "symbol": symbol.upper(),
                "interval": interval,
                "limit": limit,
            },
            quality_key="coinglassFundingRateHistory",
        )
        return self.unwrap_data(payload)

    def diagnostics(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "budget": self.budget.snapshot(),
            "quality": self.quality.snapshot(),
        }
