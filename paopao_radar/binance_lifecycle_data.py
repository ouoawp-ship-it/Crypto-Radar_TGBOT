from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, field
from queue import Queue
from threading import RLock
from time import perf_counter
from typing import Any, Callable

from .config import Settings
from .data_sources import BinanceDataSource, HTTP_HEADERS


TIMEFRAME_SECONDS = {"15m": 900, "1h": 3600, "4h": 14400, "24h": 86400}
BINANCE_PERIOD = {"15m": "15m", "1h": "1h", "4h": "4h", "24h": "1d"}
BINANCE_INTERVAL = {"15m": "15m", "1h": "1h", "4h": "4h", "24h": "1d"}
MAX_SPOT_AGG_TRADES = 1000
MAX_BATCH_WORKERS = 8
FAILURE_CACHE_TTL_SEC = 30


def safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def safe_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None:
        return None
    return round(numerator / max(denominator, 1e-12), 6)


def futures_cvd_from_taker_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    buy_total = 0.0
    sell_total = 0.0
    for row in rows:
        buy_total += safe_float(row.get("buyVol")) or 0.0
        sell_total += safe_float(row.get("sellVol")) or 0.0
    delta = buy_total - sell_total
    if delta > 0:
        status = "主动买入增强"
    elif delta < 0:
        status = "主动卖出增强"
    else:
        status = "合约主动量中性"
    return {
        "futures_cvd_delta": round(delta, 4),
        "futures_cvd_ratio": ratio(buy_total, sell_total),
        "futures_cvd_status": status,
        "buy_volume": round(buy_total, 4),
        "sell_volume": round(sell_total, 4),
        "sample_count": len(rows),
    }


def spot_cvd_from_agg_trades(rows: list[dict[str, Any]]) -> dict[str, Any]:
    buy_quote = 0.0
    sell_quote = 0.0
    for row in rows:
        price = safe_float(row.get("p")) or 0.0
        qty = safe_float(row.get("q")) or 0.0
        quote_value = price * qty
        if bool(row.get("m")):
            sell_quote += quote_value
        else:
            buy_quote += quote_value
    delta = buy_quote - sell_quote
    if delta > 0:
        status = "现货买盘跟随"
    elif delta < 0:
        status = "现货主动卖出"
    else:
        status = "现货主动量中性"
    return {
        "spot_cvd_delta": round(delta, 4),
        "spot_cvd_ratio": ratio(buy_quote, sell_quote),
        "spot_cvd_status": status,
        "spot_buy_quote": round(buy_quote, 4),
        "spot_sell_quote": round(sell_quote, 4),
    }


def kline_snapshot(rows: list[list[Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "open": None,
            "high": None,
            "low": None,
            "close": None,
            "price": None,
            "volume": None,
            "quote_volume": None,
            "close_time": None,
            "price_status": "unavailable",
        }
    row = rows[-1]
    open_price = safe_float(row[1] if len(row) > 1 else None)
    high = safe_float(row[2] if len(row) > 2 else None)
    low = safe_float(row[3] if len(row) > 3 else None)
    close = safe_float(row[4] if len(row) > 4 else None)
    volume = safe_float(row[5] if len(row) > 5 else None)
    close_time = safe_int(row[6] if len(row) > 6 else None)
    quote_volume = safe_float(row[7] if len(row) > 7 else None)
    return {
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "price": close,
        "volume": volume,
        "quote_volume": quote_volume,
        "close_time": close_time,
        "price_status": "ok" if close is not None else "unavailable",
    }


def _normalized_oi_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        timestamp = safe_int(row.get("timestamp") if row.get("timestamp") is not None else row.get("time"))
        normalized.append({
            "openInterest": safe_float(row.get("openInterest")),
            "sumOpenInterest": safe_float(row.get("sumOpenInterest")),
            "sumOpenInterestValue": safe_float(row.get("sumOpenInterestValue")),
            "timestamp": timestamp,
            "time": timestamp,
        })
    normalized.sort(key=lambda item: int(item.get("timestamp") or 0))
    return normalized


@dataclass
class BinanceLifecycleDataClient:
    settings: Settings
    source: BinanceDataSource | None = None
    cache: dict[str, tuple[float, float, Any]] = field(default_factory=dict)
    last_batch_metrics: dict[str, Any] = field(default_factory=dict)
    _cache_lock: RLock = field(default_factory=RLock, init=False, repr=False)
    _key_locks: dict[str, RLock] = field(default_factory=dict, init=False, repr=False)
    _cache_hits: int = field(default=0, init=False, repr=False)
    _cache_misses: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.source is None:
            self.source = BinanceDataSource(self.settings)

    def close(self) -> None:
        source = self.source
        http = getattr(source, "http", None)
        close = getattr(http, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> BinanceLifecycleDataClient:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def _cache_ttl(self) -> int:
        return max(1, int(getattr(self.settings, "lifecycle_binance_cache_ttl_sec", 300) or 300))

    def _timeout(self) -> int:
        return max(1, int(getattr(self.settings, "lifecycle_http_timeout_sec", self.settings.http_timeout_sec) or self.settings.http_timeout_sec))

    @staticmethod
    def _is_unavailable(value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, (list, tuple)):
            return not value
        if isinstance(value, dict):
            if not value:
                return True
            if value and all(item is None for item in value.values()):
                return True
            for key, item in value.items():
                if (key == "data_source_status" or key.endswith("_status")) and str(item).lower() == "unavailable":
                    return True
        return False

    def _cached(self, key: str) -> Any | None:
        now = time.time()
        with self._cache_lock:
            item = self.cache.get(key)
            if item is None:
                self._cache_misses += 1
                return None
            timestamp, ttl, value = item
            if now - timestamp > ttl:
                self.cache.pop(key, None)
                self._cache_misses += 1
                return None
            self._cache_hits += 1
            return deepcopy(value)

    def _store(self, key: str, value: Any) -> Any:
        normal_ttl = self._cache_ttl()
        ttl = min(normal_ttl, FAILURE_CACHE_TTL_SEC) if self._is_unavailable(value) else normal_ttl
        with self._cache_lock:
            self.cache[key] = (time.time(), float(ttl), deepcopy(value))
        return value

    def _lock_for(self, key: str) -> RLock:
        with self._cache_lock:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = RLock()
                self._key_locks[key] = lock
            return lock

    def _load_cached(self, key: str, loader: Callable[[], Any]) -> Any:
        lock = self._lock_for(key)
        with lock:
            cached = self._cached(key)
            if cached is not None:
                return cached
            return self._store(key, loader())

    def _get_json(
        self,
        url: str,
        params: dict[str, Any],
        *,
        cache_key: str,
        quality_key: str,
    ) -> Any:
        assert self.source is not None
        return self.source.http.get_json(
            url,
            params,
            cache_key=cache_key,
            quality_key=quality_key,
            timeout=self._timeout(),
            retries=1,
        )

    def _safe_component(self, loader: Callable[[], dict[str, Any]], fallback: dict[str, Any]) -> dict[str, Any]:
        try:
            value = loader()
            return value if isinstance(value, dict) else deepcopy(fallback)
        except Exception:
            return deepcopy(fallback)

    def futures_kline(self, symbol: str, timeframe: str) -> dict[str, Any]:
        assert self.source is not None
        interval = BINANCE_INTERVAL.get(timeframe, "15m")
        key = f"component:futures-kline:{symbol}:{interval}"

        def load() -> dict[str, Any]:
            rows = self._get_json(
                self.source.endpoint("/fapi/v1/klines"),
                {"symbol": symbol, "interval": interval, "limit": 2},
                cache_key=f"lifecycle:futuresKlines:{symbol}:{interval}:2",
                quality_key="lifecycleFuturesKlines",
            )
            return kline_snapshot(rows if isinstance(rows, list) else [])

        return self._load_cached(key, load)

    def spot_kline(self, symbol: str, timeframe: str) -> dict[str, Any]:
        assert self.source is not None
        interval = BINANCE_INTERVAL.get(timeframe, "15m")
        key = f"component:spot-kline:{symbol}:{interval}"

        def load() -> dict[str, Any]:
            rows = self._get_json(
                self.source.spot_endpoint("/api/v3/klines"),
                {"symbol": symbol, "interval": interval, "limit": 2},
                cache_key=f"lifecycle:spotKlines:{symbol}:{interval}:2",
                quality_key="lifecycleSpotKlines",
            )
            return kline_snapshot(rows if isinstance(rows, list) else [])

        return self._load_cached(key, load)

    def current_open_interest(self, symbol: str) -> dict[str, Any]:
        assert self.source is not None
        key = f"component:open-interest:{symbol}"

        def load() -> dict[str, Any]:
            data = self._get_json(
                self.source.endpoint("/fapi/v1/openInterest"),
                {"symbol": symbol},
                cache_key=f"lifecycle:openInterest:{symbol}",
                quality_key="lifecycleOpenInterest",
            )
            if not isinstance(data, dict):
                return {
                    "oi": None,
                    "openInterest": None,
                    "open_interest_time": None,
                    "timestamp": None,
                    "time": None,
                    "oi_status": "unavailable",
                }
            oi = safe_float(data.get("openInterest"))
            timestamp = safe_int(data.get("time") if data.get("time") is not None else data.get("timestamp"))
            return {
                "oi": oi,
                "openInterest": oi,
                "open_interest_time": timestamp,
                "timestamp": timestamp,
                "time": timestamp,
                "oi_status": "ok" if oi is not None else "unavailable",
            }

        return self._load_cached(key, load)

    def _open_interest_hist_rows(self, symbol: str, period: str, limit: int) -> list[dict[str, Any]]:
        assert self.source is not None
        data = self._get_json(
            self.source.endpoint("/futures/data/openInterestHist"),
            {"symbol": symbol, "period": period, "limit": limit},
            cache_key=f"lifecycle:openInterestHist:{symbol}:{period}:{limit}",
            quality_key=f"lifecycleOpenInterestHist:{period}",
        )
        return _normalized_oi_rows(data if isinstance(data, list) else [])

    def historical_open_interest(self, symbol: str, timeframe: str) -> dict[str, Any]:
        tf = timeframe if timeframe in TIMEFRAME_SECONDS else "15m"
        key = f"component:open-interest-history:{symbol}:{tf}"

        def load() -> dict[str, Any]:
            if tf == "24h":
                attempts = (("1d", 2, "1d"), ("4h", 6, "6x4h"), ("1h", 24, "24x1h"))
            else:
                period = BINANCE_PERIOD.get(tf, "15m")
                attempts = ((period, 2, period),)
            for period, limit, aggregation in attempts:
                rows = self._open_interest_hist_rows(symbol, period, limit)
                if not rows:
                    continue
                latest = rows[-1]
                return {
                    "oi_history_status": "ok",
                    "oi_history_timeframe": tf,
                    "oi_history_source_period": period,
                    "oi_history_aggregation": aggregation,
                    "oi_history_sample_count": len(rows),
                    "oi_history_start_time": rows[0].get("timestamp"),
                    "oi_history_end_time": latest.get("timestamp"),
                    "sum_open_interest": latest.get("sumOpenInterest"),
                    "sum_open_interest_value_usdt": latest.get("sumOpenInterestValue"),
                    "sumOpenInterest": latest.get("sumOpenInterest"),
                    "sumOpenInterestValue": latest.get("sumOpenInterestValue"),
                    "oi_history": rows,
                }
            return {
                "oi_history_status": "unavailable",
                "oi_history_timeframe": tf,
                "oi_history_source_period": None,
                "oi_history_aggregation": None,
                "oi_history_sample_count": 0,
                "oi_history": [],
            }

        return self._load_cached(key, load)

    def futures_taker_ratio(self, symbol: str, timeframe: str) -> dict[str, Any]:
        assert self.source is not None
        period = BINANCE_PERIOD.get(timeframe, "15m")
        key = f"component:futures-taker:{symbol}:{period}"

        def load() -> dict[str, Any]:
            data = self._get_json(
                self.source.endpoint("/futures/data/takerlongshortRatio"),
                {"symbol": symbol, "period": period, "limit": 1},
                cache_key=f"lifecycle:taker:{symbol}:{period}:1",
                quality_key="lifecycleTakerRatio",
            )
            if not isinstance(data, list) or not data:
                return {"futures_cvd_status": "unavailable", "futures_cvd_delta": None, "futures_cvd_ratio": None}
            return futures_cvd_from_taker_rows([row for row in data if isinstance(row, dict)])

        return self._load_cached(key, load)

    def spot_cvd(self, symbol: str, timeframe: str) -> dict[str, Any]:
        assert self.source is not None
        tf = timeframe if timeframe in TIMEFRAME_SECONDS else "15m"
        seconds = TIMEFRAME_SECONDS[tf]
        key = f"component:spot-cvd:{symbol}:{tf}"

        def load() -> dict[str, Any]:
            end_ms = int(time.time() * 1000)
            start_ms = end_ms - seconds * 1000
            data = self._get_json(
                self.source.spot_endpoint("/api/v3/aggTrades"),
                {"symbol": symbol, "startTime": start_ms, "endTime": end_ms, "limit": MAX_SPOT_AGG_TRADES},
                cache_key=f"lifecycle:spotAgg:{symbol}:{tf}:{end_ms // max(1, seconds * 1000)}",
                quality_key="lifecycleSpotAggTrades",
            )
            if not isinstance(data, list):
                return {
                    "spot_cvd_status": "unavailable",
                    "spot_cvd_delta": None,
                    "spot_cvd_collection_status": "unavailable",
                    "spot_cvd_sampled": True,
                    "spot_cvd_truncated": False,
                    "spot_cvd_trade_count": 0,
                    "spot_cvd_window_start_ms": start_ms,
                    "spot_cvd_window_end_ms": end_ms,
                }
            rows = [row for row in data if isinstance(row, dict)]
            result = spot_cvd_from_agg_trades(rows)
            timestamps = [safe_int(row.get("T")) for row in rows]
            sample_times = [value for value in timestamps if value is not None]
            result.update({
                "spot_cvd_collection_status": "sampled",
                "spot_cvd_sampled": True,
                "spot_cvd_truncated": len(rows) >= MAX_SPOT_AGG_TRADES,
                "spot_cvd_trade_count": len(rows),
                "spot_cvd_window_start_ms": start_ms,
                "spot_cvd_window_end_ms": end_ms,
                "spot_cvd_sample_start_ms": min(sample_times) if sample_times else None,
                "spot_cvd_sample_end_ms": max(sample_times) if sample_times else None,
            })
            return result

        return self._load_cached(key, load)

    def funding(self, symbol: str) -> dict[str, Any]:
        assert self.source is not None
        key = f"component:funding:{symbol}"

        def load() -> dict[str, Any]:
            # Lifecycle scans may cover 80 symbols, so this deliberately uses the
            # shared HttpClient without consuming the legacy funding-history budget (25).
            data = self._get_json(
                self.source.endpoint("/fapi/v1/fundingRate"),
                {"symbol": symbol, "limit": 1},
                cache_key=f"lifecycle:funding:{symbol}:1",
                quality_key="lifecycleFundingRate",
            )
            if not isinstance(data, list) or not data or not isinstance(data[-1], dict):
                return {"funding_status": "unavailable", "funding_rate": None}
            row = data[-1]
            rate = safe_float(row.get("fundingRate"))
            crowded_threshold = abs(float(getattr(self.settings, "lifecycle_funding_crowded_threshold", 0.0008) or 0.0008))
            if rate is None:
                status = "unavailable"
            elif rate >= crowded_threshold:
                status = "资金费率偏热"
            elif rate <= -crowded_threshold:
                status = "资金费率偏负"
            else:
                status = "未明显拥挤"
            return {
                "funding_rate": rate,
                "funding_time": safe_int(row.get("fundingTime")),
                "mark_price": safe_float(row.get("markPrice")),
                "fundingRate": rate,
                "fundingTime": safe_int(row.get("fundingTime")),
                "markPrice": safe_float(row.get("markPrice")),
                "funding_status": status,
            }

        return self._load_cached(key, load)

    def market_cap(self, symbol: str) -> dict[str, Any]:
        assert self.source is not None
        coin = symbol[:-4] if symbol.endswith("USDT") else symbol

        def load_caps() -> dict[str, Any]:
            caps = self.source.coinpaprika_market_caps() or self.source.market_caps()
            return caps if isinstance(caps, dict) else {}

        caps = self._load_cached("component:market-caps", load_caps)
        value = safe_float(caps.get(coin)) if isinstance(caps, dict) else None
        return {"market_cap_usd": value, "market_cap_status": "ok" if value else "unavailable"}

    @staticmethod
    def _first_data_item(data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            return {}
        items = data.get("data")
        if isinstance(items, list) and items and isinstance(items[0], dict):
            return items[0]
        return items if isinstance(items, dict) else {}

    @staticmethod
    def _bybit_first_item(data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            return {}
        result = data.get("result")
        items = result.get("list") if isinstance(result, dict) else None
        return items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else {}

    def _side_item(
        self,
        exchange: str,
        price: float | None,
        funding_rate: float | None,
        binance_price: float | None,
        binance_funding: float | None,
    ) -> dict[str, Any]:
        price_deviation = None
        if price is not None and binance_price is not None and abs(binance_price) > 1e-12:
            price_deviation = round((price - binance_price) / binance_price * 100, 6)
        funding_deviation = None
        if funding_rate is not None and binance_funding is not None:
            funding_deviation = round(funding_rate - binance_funding, 10)
        if price is None and funding_rate is None:
            status = "unavailable"
        elif price is None or funding_rate is None:
            status = "partial"
        else:
            status = "ok"
        return {
            "exchange": exchange,
            "current_price": price,
            "funding_rate": funding_rate,
            "price_deviation_vs_binance_pct": price_deviation,
            "funding_deviation_vs_binance": funding_deviation,
            "status": status,
        }

    def _okx_side(self, symbol: str, binance_price: float | None, binance_funding: float | None) -> dict[str, Any]:
        base = symbol[:-4] if symbol.endswith("USDT") else symbol
        inst_id = f"{base}-USDT-SWAP"

        def load() -> dict[str, Any]:
            ticker = self._get_json(
                "https://www.okx.com/api/v5/market/ticker",
                {"instId": inst_id},
                cache_key=f"lifecycle:side:okx:ticker:{inst_id}",
                quality_key="lifecycleSideOKXTicker",
            )
            funding = self._get_json(
                "https://www.okx.com/api/v5/public/funding-rate",
                {"instId": inst_id},
                cache_key=f"lifecycle:side:okx:funding:{inst_id}",
                quality_key="lifecycleSideOKXFunding",
            )
            ticker_item = self._first_data_item(ticker)
            funding_item = self._first_data_item(funding)
            return {
                "price": safe_float(ticker_item.get("last")),
                "funding": safe_float(funding_item.get("fundingRate")),
            }

        raw = self._load_cached(f"component:side:okx:{symbol}", load)
        return self._side_item("OKX", safe_float(raw.get("price")), safe_float(raw.get("funding")), binance_price, binance_funding)

    def _bybit_side(self, symbol: str, binance_price: float | None, binance_funding: float | None) -> dict[str, Any]:
        def load() -> dict[str, Any]:
            data = self._get_json(
                "https://api.bybit.com/v5/market/tickers",
                {"category": "linear", "symbol": symbol},
                cache_key=f"lifecycle:side:bybit:ticker:{symbol}",
                quality_key="lifecycleSideBybitTicker",
            )
            item = self._bybit_first_item(data)
            return {"price": safe_float(item.get("lastPrice")), "funding": safe_float(item.get("fundingRate"))}

        raw = self._load_cached(f"component:side:bybit:{symbol}", load)
        return self._side_item("Bybit", safe_float(raw.get("price")), safe_float(raw.get("funding")), binance_price, binance_funding)

    def _hyperliquid_asset_contexts(self) -> list[Any]:
        assert self.source is not None

        def load() -> list[Any]:
            try:
                response = self.source.http.session.post(
                    "https://api.hyperliquid.xyz/info",
                    json={"type": "metaAndAssetCtxs"},
                    headers=HTTP_HEADERS,
                    timeout=self._timeout(),
                )
                if int(getattr(response, "status_code", 0) or 0) != 200:
                    return []
                data = response.json()
                return data if isinstance(data, list) else []
            except Exception:
                return []

        return self._load_cached("component:side:hyperliquid:asset-contexts", load)

    def _hyperliquid_side(self, symbol: str, binance_price: float | None, binance_funding: float | None) -> dict[str, Any]:
        base = symbol[:-4] if symbol.endswith("USDT") else symbol
        data = self._hyperliquid_asset_contexts()
        price = None
        funding = None
        if len(data) >= 2 and isinstance(data[0], dict) and isinstance(data[1], list):
            universe = data[0].get("universe")
            if isinstance(universe, list):
                for index, asset in enumerate(universe):
                    if not isinstance(asset, dict) or str(asset.get("name") or "").upper() != base:
                        continue
                    context = data[1][index] if index < len(data[1]) and isinstance(data[1][index], dict) else {}
                    price = safe_float(context.get("markPx") if context.get("markPx") is not None else context.get("midPx"))
                    funding = safe_float(context.get("funding"))
                    break
        return self._side_item("Hyperliquid", price, funding, binance_price, binance_funding)

    def exchange_context(self, symbol: str, binance_price: float | None, binance_funding: float | None) -> dict[str, Any]:
        fallback = lambda exchange: self._side_item(exchange, None, None, binance_price, binance_funding)
        items = [
            self._safe_component(lambda: self._okx_side(symbol, binance_price, binance_funding), fallback("OKX")),
            self._safe_component(lambda: self._bybit_side(symbol, binance_price, binance_funding), fallback("Bybit")),
            self._safe_component(lambda: self._hyperliquid_side(symbol, binance_price, binance_funding), fallback("Hyperliquid")),
        ]
        return {
            "source": "side_observation",
            "core_exchange": "binance",
            "items": items,
            "note": "其他交易所仅作为当前价格和资金费率旁路观察，不参与生命周期评分。",
            "binance_price": binance_price,
            "binance_funding_rate": binance_funding,
        }

    def snapshot(self, symbol: str, timeframe: str = "15m") -> dict[str, Any]:
        normalized = str(symbol or "").upper().strip()
        tf = timeframe if timeframe in TIMEFRAME_SECONDS else "15m"
        cache_key = f"snapshot:{normalized}:{tf}"

        def load() -> dict[str, Any]:
            result: dict[str, Any] = {
                "symbol": normalized,
                "timeframe": tf,
                "data_source": "binance",
                "data_source_status": "ok",
            }
            unavailable_kline = kline_snapshot([])
            futures_kline = self._safe_component(
                lambda: self.futures_kline(normalized, tf),
                unavailable_kline,
            )
            result.update(futures_kline)
            result["futures_kline"] = futures_kline

            spot_kline = self._safe_component(
                lambda: self.spot_kline(normalized, tf),
                unavailable_kline,
            )
            result["spot_kline"] = spot_kline
            result["spot_price"] = safe_float(spot_kline.get("close"))
            result["spot_price_status"] = spot_kline.get("price_status", "unavailable")

            result.update(self._safe_component(
                lambda: self.current_open_interest(normalized),
                {
                    "oi": None,
                    "openInterest": None,
                    "open_interest_time": None,
                    "timestamp": None,
                    "time": None,
                    "oi_status": "unavailable",
                },
            ))
            result.update(self._safe_component(
                lambda: self.historical_open_interest(normalized, tf),
                {"oi_history_status": "unavailable", "oi_history": []},
            ))
            oi = safe_float(result.get("oi"))
            price = safe_float(result.get("price"))
            result["oi_value_usdt"] = round(oi * price, 4) if oi is not None and price is not None else None

            result.update(self._safe_component(
                lambda: self.futures_taker_ratio(normalized, tf),
                {"futures_cvd_status": "unavailable", "futures_cvd_delta": None, "futures_cvd_ratio": None},
            ))
            result.update(self._safe_component(
                lambda: self.spot_cvd(normalized, tf),
                {"spot_cvd_status": "unavailable", "spot_cvd_delta": None, "spot_cvd_collection_status": "unavailable"},
            ))
            result.update(self._safe_component(
                lambda: self.funding(normalized),
                {"funding_status": "unavailable", "funding_rate": None},
            ))
            result.update(self._safe_component(
                lambda: self.market_cap(normalized),
                {"market_cap_status": "unavailable", "market_cap_usd": None},
            ))
            result["exchange_context"] = self.exchange_context(
                normalized,
                safe_float(result.get("price")),
                safe_float(result.get("funding_rate")),
            )
            unavailable_keys = [
                key
                for key in (
                    "price_status",
                    "oi_status",
                    "oi_history_status",
                    "futures_cvd_status",
                    "spot_cvd_status",
                    "funding_status",
                )
                if result.get(key) == "unavailable"
            ]
            result["unavailable_components"] = unavailable_keys
            if result.get("price") is None:
                result["data_source_status"] = "unavailable"
                result["data_source_reason"] = "Binance 暂无该交易对生命周期行情数据。"
            return result

        try:
            return self._load_cached(cache_key, load)
        except Exception:
            fallback = {
                "symbol": normalized,
                "timeframe": tf,
                "data_source": "binance",
                "data_source_status": "unavailable",
                "data_source_reason": "Binance 生命周期行情暂不可用。",
                "exchange_context": self.exchange_context(normalized, None, None),
            }
            return self._store(cache_key, fallback)

    def snapshot_many(
        self,
        pairs: list[tuple[str, str]],
        max_workers: int = MAX_BATCH_WORKERS,
    ) -> dict[tuple[str, str], dict[str, Any]]:
        symbol_limit = max(1, int(getattr(self.settings, "lifecycle_active_max_symbols", 80) or 80))
        normalized_pairs: list[tuple[str, str]] = []
        selected_symbols: set[str] = set()
        seen_pairs: set[tuple[str, str]] = set()
        for raw_symbol, raw_timeframe in pairs:
            symbol = str(raw_symbol or "").upper().strip()
            if not symbol:
                continue
            timeframe = raw_timeframe if raw_timeframe in TIMEFRAME_SECONDS else "15m"
            pair = (symbol, timeframe)
            if pair in seen_pairs:
                continue
            if symbol not in selected_symbols and len(selected_symbols) >= symbol_limit:
                continue
            selected_symbols.add(symbol)
            seen_pairs.add(pair)
            normalized_pairs.append(pair)

        started = perf_counter()
        before_hits = self._cache_hits
        before_misses = self._cache_misses
        if not normalized_pairs:
            self.last_batch_metrics = {
                "pairs": 0,
                "symbols": 0,
                "succeeded": 0,
                "unavailable": 0,
                "max_workers": 0,
                "peak_concurrency": 0,
                "elapsed_sec": 0.0,
                "cache_hits": 0,
                "cache_misses": 0,
            }
            return {}

        worker_count = min(MAX_BATCH_WORKERS, max(1, int(max_workers or MAX_BATCH_WORKERS)), len(normalized_pairs))
        queue: Queue[tuple[str, str] | None] = Queue()
        for pair in normalized_pairs:
            queue.put(pair)
        for _ in range(worker_count):
            queue.put(None)

        results: dict[tuple[str, str], dict[str, Any]] = {}
        result_lock = RLock()
        active = 0
        peak_active = 0

        def worker() -> None:
            nonlocal active, peak_active
            while True:
                pair = queue.get()
                if pair is None:
                    return
                with result_lock:
                    active += 1
                    peak_active = max(peak_active, active)
                try:
                    result = self.snapshot(*pair)
                except Exception:
                    result = {
                        "symbol": pair[0],
                        "timeframe": pair[1],
                        "data_source": "binance",
                        "data_source_status": "unavailable",
                        "data_source_reason": "Binance 生命周期行情暂不可用。",
                    }
                finally:
                    with result_lock:
                        active -= 1
                with result_lock:
                    results[pair] = result

        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="lifecycle-binance") as executor:
            workers = [executor.submit(worker) for _ in range(worker_count)]
            for future in workers:
                future.result()

        elapsed = perf_counter() - started
        unavailable = sum(1 for item in results.values() if item.get("data_source_status") == "unavailable")
        self.last_batch_metrics = {
            "pairs": len(normalized_pairs),
            "symbols": len(selected_symbols),
            "succeeded": len(results) - unavailable,
            "unavailable": unavailable,
            "max_workers": worker_count,
            "peak_concurrency": peak_active,
            "elapsed_sec": round(elapsed, 4),
            "cache_hits": max(0, self._cache_hits - before_hits),
            "cache_misses": max(0, self._cache_misses - before_misses),
        }
        return {pair: results[pair] for pair in normalized_pairs if pair in results}
