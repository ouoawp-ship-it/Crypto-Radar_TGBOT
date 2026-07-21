from __future__ import annotations

import math
from typing import Any

from .config import Settings
from .data_sources import DataQuality, HttpClient


CROSS_EXCHANGE_OI_SCHEMA_VERSION = "workstation.funds.open-interest.v1"
FUNDS_PROFILE_SCHEMA_VERSION = "workstation.funds.profile.v1"


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _positive(value: Any) -> float | None:
    number = _number(value)
    return number if number is not None and number > 0 else None


def _first(value: Any, *path: str) -> dict[str, Any]:
    current = value
    for key in path:
        current = current.get(key) if isinstance(current, dict) else None
    if isinstance(current, list) and current and isinstance(current[0], dict):
        return current[0]
    return current if isinstance(current, dict) else {}


def _rounded(value: float | None, digits: int = 6) -> float | None:
    return round(value, digits) if value is not None and math.isfinite(value) else None


def build_funds_series_analytics(
    points: list[dict[str, Any]],
    *,
    metric: str,
    interval_sec: int,
) -> dict[str, Any]:
    safe_interval = max(1, int(interval_sec or 1))
    ordered = [point for point in points if isinstance(point, dict)]
    flows = [_number(point.get(metric)) for point in ordered]
    prices = [_positive(point.get("price")) for point in ordered]
    valid_flows = [value for value in flows if value is not None]
    valid_prices = [value for value in prices if value is not None]
    net_flow = sum(valid_flows) if valid_flows else None
    direction = "neutral"
    if net_flow is not None and net_flow > 0:
        direction = "inflow"
    elif net_flow is not None and net_flow < 0:
        direction = "outflow"

    latest_direction = 0
    duration_buckets = 0
    for value in reversed(flows):
        if value is None or value == 0:
            break
        sign = 1 if value > 0 else -1
        if latest_direction == 0:
            latest_direction = sign
        if sign != latest_direction:
            break
        duration_buckets += 1

    backtest_samples = 0
    backtest_hits = 0
    for index in range(max(0, len(ordered) - 1)):
        flow = flows[index]
        current_price = prices[index]
        next_price = prices[index + 1]
        if flow in {None, 0} or current_price is None or next_price is None or current_price == next_price:
            continue
        backtest_samples += 1
        if (flow > 0) == (next_price > current_price):
            backtest_hits += 1

    first_price = valid_prices[0] if valid_prices else None
    last_price = valid_prices[-1] if valid_prices else None
    price_change_pct = (
        (last_price / first_price - 1) * 100
        if first_price is not None and last_price is not None and first_price > 0
        else None
    )
    return {
        "data_status": "ready" if len(valid_flows) >= 2 and len(valid_prices) >= 2 else "degraded" if valid_flows or valid_prices else "unavailable",
        "metric": metric,
        "net_flow_usd": _rounded(net_flow, 2),
        "direction": direction,
        "latest_direction": "inflow" if latest_direction > 0 else "outflow" if latest_direction < 0 else "neutral",
        "duration_sec": duration_buckets * safe_interval,
        "hit_rate_pct": _rounded(backtest_hits / backtest_samples * 100, 4) if backtest_samples else None,
        "hit_samples": backtest_samples,
        "price": {
            "first": _rounded(first_price),
            "current": _rounded(last_price),
            "change_pct": _rounded(price_change_pct, 4),
            "high": _rounded(max(valid_prices)) if valid_prices else None,
            "low": _rounded(min(valid_prices)) if valid_prices else None,
        },
        "coverage": {"points": len(ordered), "flow": len(valid_flows), "price": len(valid_prices)},
        "methodology": {
            "direction": "所选闭合桶内主动买入额减主动卖出额求和；正值为流入，负值为流出。",
            "duration": "从最新闭合桶向前统计同方向且非零的连续桶数，再乘所选桶周期。",
            "hit_rate": "每个可用桶用当期资金方向预测下一桶价格方向；价格不变、资金为零或缺失的样本不计入分母。",
        },
    }


def build_volume_profile(
    points: list[dict[str, Any]],
    *,
    bin_count: int = 24,
    value_area_ratio: float = 0.7,
) -> dict[str, Any]:
    rows = []
    for point in points:
        if not isinstance(point, dict):
            continue
        high = _positive(point.get("high"))
        low = _positive(point.get("low"))
        close = _positive(point.get("close"))
        volume = _positive(point.get("quote_volume"))
        if high is None or low is None or close is None or volume is None or high < low:
            continue
        rows.append((high, low, close, volume))
    if not rows:
        return {
            "data_status": "unavailable",
            "poc": None,
            "vah": None,
            "val": None,
            "coverage": {"points": 0, "bins": 0},
            "methodology": "仅使用带高低收与美元成交额的闭合 K 线；样本不足时不生成关键价位。",
        }

    safe_bins = max(8, min(80, int(bin_count or 24)))
    ratio = max(0.5, min(0.95, float(value_area_ratio or 0.7)))
    range_low = min(row[1] for row in rows)
    range_high = max(row[0] for row in rows)
    if range_high <= range_low:
        return {
            "data_status": "degraded",
            "poc": _rounded(range_low),
            "vah": _rounded(range_high),
            "val": _rounded(range_low),
            "coverage": {"points": len(rows), "bins": 1},
            "methodology": "所有闭合 K 线价格相同，POC、VAH 与 VAL 退化为同一价格。",
        }

    width = (range_high - range_low) / safe_bins
    volumes = [0.0] * safe_bins
    for high, low, close, volume in rows:
        typical = (high + low + close) / 3
        index = min(safe_bins - 1, max(0, int((typical - range_low) / width)))
        volumes[index] += volume
    poc_index = max(range(safe_bins), key=lambda index: volumes[index])
    selected = {poc_index}
    accumulated = volumes[poc_index]
    target = sum(volumes) * ratio
    left = poc_index - 1
    right = poc_index + 1
    while accumulated < target and (left >= 0 or right < safe_bins):
        left_volume = volumes[left] if left >= 0 else -1
        right_volume = volumes[right] if right < safe_bins else -1
        if right_volume > left_volume:
            selected.add(right)
            accumulated += max(0, right_volume)
            right += 1
        else:
            selected.add(left)
            accumulated += max(0, left_volume)
            left -= 1
    return {
        "data_status": "ready" if len(rows) >= 24 else "degraded",
        "poc": _rounded(range_low + (poc_index + 0.5) * width),
        "vah": _rounded(range_low + (max(selected) + 1) * width),
        "val": _rounded(range_low + min(selected) * width),
        "range_high": _rounded(range_high),
        "range_low": _rounded(range_low),
        "value_area_ratio": ratio,
        "coverage": {"points": len(rows), "bins": safe_bins},
        "methodology": "按典型价 (H+L+C)/3 将每根闭合 K 线的美元成交额分配到价格桶；POC 为最大成交量桶中心，VAH/VAL 为从 POC 向两侧扩展得到的 70% 成交量价值区边界。",
    }


def build_cross_exchange_open_interest(
    symbol: str,
    *,
    mark_price: float | None,
    payloads: dict[str, Any],
) -> dict[str, Any]:
    target = str(symbol or "").upper()
    price = _positive(mark_price)
    binance_raw = payloads.get("binance") if isinstance(payloads.get("binance"), dict) else {}
    bybit_raw = _first(payloads.get("bybit"), "result", "list")
    okx_raw = _first(payloads.get("okx"), "data")

    binance_native = _positive(binance_raw.get("openInterest"))
    bybit_native = _positive(bybit_raw.get("openInterest"))
    okx_native = _positive(okx_raw.get("oiCcy")) or _positive(okx_raw.get("oi"))
    values = {
        "binance": binance_native * price if binance_native is not None and price is not None else None,
        "bybit": bybit_native * price if bybit_native is not None and price is not None else None,
        "okx": _positive(okx_raw.get("oiUsd"))
        or (okx_native * price if okx_native is not None and price is not None else None),
    }
    natives = {"binance": binance_native, "bybit": bybit_native, "okx": okx_native}
    sources = {
        "binance": "binance_fapi_open_interest",
        "bybit": "bybit_v5_open_interest",
        "okx": "okx_public_open_interest",
    }
    available_values = [float(value) for value in values.values() if value is not None]
    total = sum(available_values) if available_values else None
    rows = []
    for exchange in ("binance", "bybit", "okx"):
        value = values[exchange]
        rows.append({
            "exchange": exchange,
            "oi_usd": round(value, 2) if value is not None else None,
            "oi_native": natives[exchange],
            "share_pct": round(value / total * 100, 4) if value is not None and total else None,
            "status": "ready" if value is not None else "unavailable",
            "source": sources[exchange],
        })
    ready = sum(1 for row in rows if row["status"] == "ready")
    top_share = max((float(row["share_pct"]) for row in rows if row["share_pct"] is not None), default=None)
    return {
        "schema_version": CROSS_EXCHANGE_OI_SCHEMA_VERSION,
        "symbol": target,
        "data_status": "ready" if ready >= 2 else "degraded" if ready else "unavailable",
        "coverage": {"exchanges": ready, "target": len(rows)},
        "mark_price": price,
        "total_oi_usd": round(total, 2) if total is not None else None,
        "top_exchange_share_pct": round(top_share, 4) if top_share is not None else None,
        "exchanges": rows,
        "methodology": {
            "normalization": "Binance/Bybit base-asset OI uses the current Binance mark price; OKX prefers its reported oiUsd.",
            "concentration": "Exchange share equals venue OI USD divided by the available-venue total; missing venues are excluded, never treated as zero.",
        },
    }


def collect_cross_exchange_open_interest(
    settings: Settings,
    symbol: str,
    *,
    http: HttpClient | None = None,
) -> dict[str, Any]:
    target = str(symbol or "").upper()
    own_http = http is None
    client = http or HttpClient(settings, DataQuality())
    try:
        mark = client.get_json(
            f"{settings.binance_fapi_base_url.rstrip('/')}/fapi/v1/premiumIndex",
            {"symbol": target},
            cache_key=f"workstation:oi:mark:{target}",
            quality_key="binanceOpenInterest",
            retries=1,
        )
        payloads = {
            "binance": client.get_json(
                f"{settings.binance_fapi_base_url.rstrip('/')}/fapi/v1/openInterest",
                {"symbol": target},
                cache_key=f"workstation:oi:binance:{target}",
                quality_key="binanceOpenInterest",
                retries=1,
            ),
            "bybit": client.get_json(
                f"{settings.bybit_public_rest_url.rstrip('/')}/v5/market/open-interest",
                {"category": "linear", "symbol": target, "intervalTime": "5min", "limit": 1},
                cache_key=f"workstation:oi:bybit:{target}",
                quality_key="bybitOpenInterest",
                retries=1,
            ),
            "okx": client.get_json(
                f"{settings.okx_public_rest_url.rstrip('/')}/api/v5/public/open-interest",
                {"instType": "SWAP", "instId": f"{target[:-4]}-USDT-SWAP"},
                cache_key=f"workstation:oi:okx:{target}",
                quality_key="okxOpenInterest",
                retries=1,
            ),
        }
        return build_cross_exchange_open_interest(
            target,
            mark_price=_positive(mark.get("markPrice")) if isinstance(mark, dict) else None,
            payloads=payloads,
        )
    finally:
        if own_http:
            client.close()


__all__ = [
    "CROSS_EXCHANGE_OI_SCHEMA_VERSION",
    "FUNDS_PROFILE_SCHEMA_VERSION",
    "build_cross_exchange_open_interest",
    "build_funds_series_analytics",
    "build_volume_profile",
    "collect_cross_exchange_open_interest",
]
