from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .config import Settings
from .data_sources import BinanceDataSource


TIMEFRAME_SECONDS = {"15m": 900, "1h": 3600, "4h": 14400, "24h": 86400}
BINANCE_PERIOD = {"15m": "15m", "1h": "1h", "4h": "4h", "24h": "1d"}
BINANCE_INTERVAL = {"15m": "15m", "1h": "1h", "4h": "4h", "24h": "1d"}


def safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or abs(denominator) < 1e-12:
        return None
    return round(numerator / denominator, 6)


def futures_cvd_from_taker_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    buy_total = 0.0
    sell_total = 0.0
    latest_ratio: float | None = None
    for row in rows:
        buy_total += safe_float(row.get("buyVol")) or 0.0
        sell_total += safe_float(row.get("sellVol")) or 0.0
        latest_ratio = safe_float(row.get("buySellRatio")) or latest_ratio
    delta = buy_total - sell_total
    computed_ratio = ratio(buy_total, sell_total)
    if delta > 0:
        status = "主动买入增强"
    elif delta < 0:
        status = "主动卖出增强"
    else:
        status = "合约主动量中性"
    return {
        "futures_cvd_delta": round(delta, 4),
        "futures_cvd_ratio": computed_ratio if computed_ratio is not None else latest_ratio,
        "futures_cvd_status": status,
        "buy_volume": round(buy_total, 4),
        "sell_volume": round(sell_total, 4),
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
        return {"price_status": "unavailable"}
    row = rows[-1]
    price = safe_float(row[4] if len(row) > 4 else None)
    volume = safe_float(row[5] if len(row) > 5 else None)
    quote_volume = safe_float(row[7] if len(row) > 7 else None)
    high = safe_float(row[2] if len(row) > 2 else None)
    low = safe_float(row[3] if len(row) > 3 else None)
    return {
        "price": price,
        "volume": volume,
        "quote_volume": quote_volume,
        "high": high,
        "low": low,
        "close_time": int(row[6]) if len(row) > 6 and row[6] is not None else None,
        "price_status": "ok" if price is not None else "unavailable",
    }


@dataclass
class BinanceLifecycleDataClient:
    settings: Settings
    source: BinanceDataSource | None = None
    cache: dict[str, tuple[float, dict[str, Any]]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.source is None:
            self.source = BinanceDataSource(self.settings)

    def _cache_ttl(self) -> int:
        return max(1, int(getattr(self.settings, "lifecycle_binance_cache_ttl_sec", 300) or 300))

    def _cached(self, key: str) -> dict[str, Any] | None:
        item = self.cache.get(key)
        if not item:
            return None
        ts, value = item
        if time.time() - ts <= self._cache_ttl():
            return dict(value)
        return None

    def _store(self, key: str, value: dict[str, Any]) -> dict[str, Any]:
        self.cache[key] = (time.time(), dict(value))
        return value

    def current_open_interest(self, symbol: str) -> dict[str, Any]:
        assert self.source is not None
        data = self.source.http.get_json(
            self.source.endpoint("/fapi/v1/openInterest"),
            {"symbol": symbol},
            cache_key=f"lifecycle:openInterest:{symbol}",
            quality_key="lifecycleOpenInterest",
            timeout=getattr(self.settings, "lifecycle_http_timeout_sec", self.settings.http_timeout_sec),
            retries=1,
        )
        if not isinstance(data, dict):
            return {"oi_status": "unavailable"}
        oi = safe_float(data.get("openInterest"))
        return {"oi": oi, "oi_status": "ok" if oi is not None else "unavailable"}

    def futures_taker_ratio(self, symbol: str, timeframe: str) -> dict[str, Any]:
        assert self.source is not None
        period = BINANCE_PERIOD.get(timeframe, "15m")
        data = self.source.http.get_json(
            self.source.endpoint("/futures/data/takerlongshortRatio"),
            {"symbol": symbol, "period": period, "limit": 4},
            cache_key=f"lifecycle:taker:{symbol}:{period}",
            quality_key="lifecycleTakerRatio",
            timeout=getattr(self.settings, "lifecycle_http_timeout_sec", self.settings.http_timeout_sec),
            retries=1,
        )
        if not isinstance(data, list):
            return {"futures_cvd_status": "unavailable", "futures_cvd_delta": None}
        return futures_cvd_from_taker_rows([row for row in data if isinstance(row, dict)])

    def spot_cvd(self, symbol: str, timeframe: str) -> dict[str, Any]:
        assert self.source is not None
        seconds = TIMEFRAME_SECONDS.get(timeframe, 900)
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - seconds * 1000
        data = self.source.http.get_json(
            self.source.spot_endpoint("/api/v3/aggTrades"),
            {"symbol": symbol, "startTime": start_ms, "endTime": end_ms, "limit": 1000},
            cache_key=f"lifecycle:spotAgg:{symbol}:{timeframe}:{end_ms // max(1, seconds * 1000)}",
            quality_key="lifecycleSpotAggTrades",
            timeout=getattr(self.settings, "lifecycle_http_timeout_sec", self.settings.http_timeout_sec),
            retries=1,
        )
        if not isinstance(data, list):
            return {"spot_cvd_status": "unavailable", "spot_cvd_delta": None}
        return spot_cvd_from_agg_trades([row for row in data if isinstance(row, dict)])

    def funding(self, symbol: str) -> dict[str, Any]:
        assert self.source is not None
        rows = self.source.funding_rate(symbol, limit=1)
        if not rows:
            return {"funding_status": "unavailable", "funding_rate": None}
        row = rows[-1]
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
            "funding_time": row.get("fundingTime"),
            "mark_price": safe_float(row.get("markPrice")),
            "funding_status": status,
        }

    def market_cap(self, symbol: str) -> dict[str, Any]:
        assert self.source is not None
        coin = symbol[:-4] if symbol.endswith("USDT") else symbol
        caps = self.source.coinpaprika_market_caps() or self.source.market_caps()
        value = safe_float(caps.get(coin)) if isinstance(caps, dict) else None
        return {"market_cap_usd": value, "market_cap_status": "ok" if value else "unavailable"}

    def exchange_context(self, symbol: str, binance_price: float | None, binance_funding: float | None) -> dict[str, Any]:
        # v1.76 keeps non-Binance venues as a side channel only. Concrete connectors can fill this later
        # without changing lifecycle scoring semantics.
        return {
            "source": "side_observation",
            "core_exchange": "binance",
            "items": [],
            "note": "其他交易所仅作为当前价格和资金费率旁路观察，不参与生命周期评分。",
            "binance_price": binance_price,
            "binance_funding_rate": binance_funding,
        }

    def snapshot(self, symbol: str, timeframe: str = "15m") -> dict[str, Any]:
        normalized = str(symbol or "").upper()
        tf = timeframe if timeframe in TIMEFRAME_SECONDS else "15m"
        cache_key = f"snapshot:{normalized}:{tf}"
        cached = self._cached(cache_key)
        if cached is not None:
            return cached
        assert self.source is not None
        result: dict[str, Any] = {
            "symbol": normalized,
            "timeframe": tf,
            "data_source": "binance",
            "data_source_status": "ok",
        }
        try:
            interval = BINANCE_INTERVAL.get(tf, "15m")
            result.update(kline_snapshot(self.source.klines(normalized, interval=interval, limit=2)))
            result.update(self.current_open_interest(normalized))
            oi = safe_float(result.get("oi"))
            price = safe_float(result.get("price"))
            result["oi_value_usdt"] = round(oi * price, 4) if oi is not None and price is not None else None
            result.update(self.futures_taker_ratio(normalized, tf))
            result.update(self.spot_cvd(normalized, tf))
            result.update(self.funding(normalized))
            result.update(self.market_cap(normalized))
            result["exchange_context"] = self.exchange_context(
                normalized,
                safe_float(result.get("price")),
                safe_float(result.get("funding_rate")),
            )
            unavailable_keys = [key for key in ("price_status", "oi_status", "futures_cvd_status", "spot_cvd_status", "funding_status") if result.get(key) == "unavailable"]
            if unavailable_keys and result.get("price") is None:
                result["data_source_status"] = "unavailable"
                result["data_source_reason"] = "Binance 暂无该交易对生命周期行情数据。"
        except Exception as exc:
            result["data_source_status"] = "unavailable"
            result["data_source_reason"] = f"{type(exc).__name__}: {exc}"[:180]
        return self._store(cache_key, result)
