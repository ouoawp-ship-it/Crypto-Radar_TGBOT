from __future__ import annotations

import time
from typing import Any


DAY_MS = 86_400_000


def _number(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if parsed == parsed and abs(parsed) != float("inf") else 0.0


def _closed_daily_rows(
    rows: list[list[Any]],
    *,
    now_ms: int,
) -> list[dict[str, float]]:
    normalized: dict[int, dict[str, float]] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, (list, tuple)) or len(row) < 8:
            continue
        open_time = int(_number(row[0]))
        close_time = int(_number(row[6]))
        high = _number(row[2])
        low = _number(row[3])
        close = _number(row[4])
        quote_volume = _number(row[7])
        if (
            open_time <= 0
            or close_time <= 0
            or close_time > now_ms
            or high <= 0
            or low <= 0
            or close <= 0
            or quote_volume < 0
        ):
            continue
        normalized[open_time] = {
            "open_time": float(open_time),
            "close_time": float(close_time),
            "high": high,
            "low": low,
            "close": close,
            "quote_volume": quote_volume,
        }
    return [normalized[key] for key in sorted(normalized)]


def _linear_regression_cumulative_slope_pct(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    count = len(values)
    mean_x = (count - 1) / 2.0
    mean_y = sum(values) / count
    denominator = sum((index - mean_x) ** 2 for index in range(count))
    if denominator <= 0 or mean_y <= 0:
        return 0.0
    slope = sum(
        (index - mean_x) * (value - mean_y)
        for index, value in enumerate(values)
    ) / denominator
    return slope * (count - 1) / mean_y * 100.0


def analyze_accumulation_quality(
    daily_klines: list[list[Any]],
    *,
    now_ms: int | None = None,
    min_history_days: int = 45,
    max_range_pct: float = 80.0,
    max_abs_slope_pct: float = 20.0,
    max_avg_daily_quote_volume: float = 20_000_000,
    recent_days: int = 7,
    max_recent_price_gain_pct: float = 300.0,
) -> dict[str, Any]:
    observed_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    closed = _closed_daily_rows(daily_klines, now_ms=observed_ms)
    required_baseline = max(1, int(min_history_days))
    recent_count = max(1, int(recent_days))
    required_total = required_baseline + recent_count
    result: dict[str, Any] = {
        "eligible": False,
        "exclusion_reason": "",
        "history_days": len(closed),
        "required_history_days": required_total,
        "sideways_days": 0,
        "range_pct": 0.0,
        "slope_pct": 0.0,
        "average_daily_quote_volume": 0.0,
        "recent_volume_ratio": 0.0,
        "recent_price_gain_pct": 0.0,
        "data_source": "Binance USDⓈ-M Futures 已闭合1d K线",
        "observed_at": observed_ms // 1000,
    }
    if len(closed) < required_total:
        result["exclusion_reason"] = "insufficient_history"
        return result

    baseline = closed[:-recent_count]
    recent = closed[-recent_count:]
    if not baseline:
        result["exclusion_reason"] = "insufficient_baseline"
        return result

    lows = [row["low"] for row in baseline]
    highs = [row["high"] for row in baseline]
    closes = [row["close"] for row in baseline]
    baseline_volumes = [row["quote_volume"] for row in baseline]
    recent_volumes = [row["quote_volume"] for row in recent]
    low = min(lows)
    range_pct = ((max(highs) / low) - 1.0) * 100.0 if low > 0 else 0.0
    slope_pct = _linear_regression_cumulative_slope_pct(closes)
    average_volume = sum(baseline_volumes) / len(baseline_volumes)
    recent_average_volume = sum(recent_volumes) / len(recent_volumes)
    recent_volume_ratio = (
        recent_average_volume / average_volume if average_volume > 0 else 0.0
    )
    baseline_average_price = sum(closes) / len(closes)
    recent_average_price = sum(row["close"] for row in recent) / len(recent)
    recent_price_gain_pct = (
        ((recent_average_price / baseline_average_price) - 1.0) * 100.0
        if baseline_average_price > 0
        else 0.0
    )
    result.update({
        "sideways_days": len(baseline),
        "range_pct": round(range_pct, 4),
        "slope_pct": round(slope_pct, 4),
        "average_daily_quote_volume": round(average_volume, 2),
        "recent_volume_ratio": round(recent_volume_ratio, 4),
        "recent_price_gain_pct": round(recent_price_gain_pct, 4),
    })

    if range_pct > float(max_range_pct):
        result["exclusion_reason"] = "range_too_wide"
        return result
    if abs(slope_pct) > abs(float(max_abs_slope_pct)):
        result["exclusion_reason"] = "trend_too_strong"
        return result
    if average_volume > float(max_avg_daily_quote_volume):
        result["exclusion_reason"] = "baseline_volume_too_high"
        return result
    if recent_price_gain_pct > float(max_recent_price_gain_pct):
        result["exclusion_reason"] = "recent_price_already_extended"
        return result

    result["eligible"] = True
    result["exclusion_reason"] = ""
    return result


def accumulation_quality_allows_ambush(
    quality: dict[str, Any],
    *,
    min_baseline_days: int,
) -> bool:
    return (
        bool(quality.get("eligible"))
        and int(quality.get("sideways_days") or 0) >= max(1, int(min_baseline_days))
    )


def format_accumulation_evidence(quality: dict[str, Any]) -> str:
    average_volume = _number(quality.get("average_daily_quote_volume"))
    if average_volume >= 1_000_000_000:
        volume_text = f"${average_volume / 1_000_000_000:.1f}B"
    elif average_volume >= 1_000_000:
        volume_text = f"${average_volume / 1_000_000:.0f}M"
    elif average_volume >= 1_000:
        volume_text = f"${average_volume / 1_000:.0f}K"
    else:
        volume_text = f"${average_volume:.0f}"
    return (
        "质量V2: "
        f"横盘{int(quality.get('sideways_days') or 0)}天 · "
        f"区间{_number(quality.get('range_pct')):.1f}% · "
        f"斜率{_number(quality.get('slope_pct')):+.1f}% · "
        f"均量{volume_text} · "
        f"近7日放量{_number(quality.get('recent_volume_ratio')):.2f}x · "
        "来源 Binance已闭合日K"
    )
