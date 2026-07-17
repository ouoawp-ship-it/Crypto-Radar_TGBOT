from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any


COIN_EVIDENCE_SCHEMA_VERSION = "2026-07-17"
CHART_INTERVALS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}
CHART_MARKETS = ("spot", "futures")


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _iso_millis(value: Any) -> str:
    number = _number(value)
    if number is None or number <= 0:
        return ""
    seconds = number / 1000 if number >= 10_000_000_000 else number
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_chart_interval(value: Any) -> str:
    requested = str(value or "15m").strip().lower()
    return requested if requested in CHART_INTERVALS else "15m"


def normalize_chart_market(value: Any) -> str:
    requested = str(value or "futures").strip().lower()
    return requested if requested in CHART_MARKETS else "futures"


def build_kline_chart(
    klines: list[list[Any]],
    *,
    market_type: str,
    interval: str,
    requested: int,
) -> dict[str, Any]:
    safe_market = normalize_chart_market(market_type)
    safe_interval = normalize_chart_interval(interval)
    safe_requested = max(24, min(240, int(requested or 96)))
    points: list[dict[str, Any]] = []
    for item in klines[-safe_requested:]:
        if not isinstance(item, list) or len(item) < 8:
            continue
        open_price = _number(item[1])
        high = _number(item[2])
        low = _number(item[3])
        close = _number(item[4])
        if None in {open_price, high, low, close}:
            continue
        if min(float(open_price), float(high), float(low), float(close)) <= 0:
            continue
        points.append({
            "open_time": _iso_millis(item[0]),
            "open_time_ms": int(_number(item[0]) or 0),
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "base_volume": _number(item[5]) if len(item) > 5 else None,
            "close_time_ms": int(_number(item[6]) or 0) if len(item) > 6 else 0,
            "quote_volume": _number(item[7]) if len(item) > 7 else None,
            "taker_buy_quote_volume": _number(item[10]) if len(item) > 10 else None,
        })
    source = "binance_spot_klines" if safe_market == "spot" else "binance_futures_klines"
    return {
        "market_type": safe_market,
        "interval": safe_interval,
        "interval_sec": CHART_INTERVALS[safe_interval],
        "source": source,
        "data_status": "ready" if len(points) >= min(24, safe_requested) else "degraded" if points else "unavailable",
        "coverage": {"requested": safe_requested, "returned": len(points)},
        "points": points,
        "warnings": [] if points else ["当前市场或周期的 K 线暂时不可用。"],
    }


def build_snapshot_series(points: list[dict[str, Any]]) -> dict[str, Any]:
    allowed = (
        "observed_at",
        "updated_at",
        "price",
        "quote_volume",
        "market_cap",
        "oi_usd",
        "oi_change_pct",
        "spot_inflow_usd",
        "spot_outflow_usd",
        "spot_flow_usd",
        "futures_inflow_usd",
        "futures_outflow_usd",
        "futures_flow_usd",
        "funding_pct",
        "sources",
    )
    safe_points = [
        {key: item.get(key) for key in allowed if item.get(key) is not None}
        for item in points[-600:]
        if isinstance(item, dict)
    ]
    coverage = {
        "points": len(safe_points),
        "price": sum(1 for item in safe_points if item.get("price") is not None),
        "oi": sum(1 for item in safe_points if item.get("oi_usd") is not None),
        "spot_flow": sum(1 for item in safe_points if item.get("spot_flow_usd") is not None),
        "futures_flow": sum(1 for item in safe_points if item.get("futures_flow_usd") is not None),
        "funding": sum(1 for item in safe_points if item.get("funding_pct") is not None),
    }
    available_metrics = sum(1 for key in ("price", "oi", "spot_flow", "futures_flow", "funding") if coverage[key] > 0)
    status = "ready" if len(safe_points) >= 2 and available_metrics >= 4 else "degraded" if safe_points else "unavailable"
    warnings: list[str] = []
    if coverage["points"] < 2:
        warnings.append("市场快照尚未积累成可比序列。")
    if coverage["spot_flow"] == 0 or coverage["futures_flow"] == 0:
        warnings.append("资金流序列需要资金流雷达持续运行后才能累积。")
    return {
        "data_status": status,
        "coverage": coverage,
        "points": safe_points,
        "warnings": warnings,
        "methodology": {
            "price_oi_funding": "按服务端市场快照时间戳输出，不在前端插值。",
            "flow": "现货和合约资金为封闭 K 线窗口的主动买卖成交差（CVD）。",
        },
    }


__all__ = [
    "CHART_INTERVALS",
    "CHART_MARKETS",
    "COIN_EVIDENCE_SCHEMA_VERSION",
    "build_kline_chart",
    "build_snapshot_series",
    "normalize_chart_interval",
    "normalize_chart_market",
]
