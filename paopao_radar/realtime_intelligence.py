from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from datetime import datetime, timezone
from statistics import mean, median
from typing import Any


REALTIME_INTELLIGENCE_SCHEMA_VERSION = "2026-07-18.2"
MOMENTUM_WINDOWS = (("15m", 900), ("30m", 1_800), ("1h", 3_600), ("4h", 14_400), ("1d", 86_400))
INTELLIGENCE_WINDOWS = (("5m", 300), *MOMENTUM_WINDOWS)
BACKTEST_HORIZONS = (("5m", 300), ("15m", 900), ("1h", 3600))
MIN_WINDOW_COVERAGE = 0.60
MIN_BACKTEST_SAMPLES = 30

ANOMALY_EVENT_WINDOWS = (("5m", 300), ("15m", 900), ("1h", 3_600))
ANOMALY_PRICE_THRESHOLDS = {"5m": 0.6, "15m": 1.0, "1h": 2.0}
ANOMALY_VOLUME_THRESHOLDS = {"5m": 80.0, "15m": 60.0, "1h": 40.0}
ANOMALY_FLOW_THRESHOLDS = {"5m": 8.0, "15m": 6.0, "1h": 4.0}


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _iso_seconds(value: int) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z") if value > 0 else ""


def _bucket_end(row: dict[str, Any]) -> int:
    return int(row.get("bucket_start") or 0) + max(1, int(row.get("bucket_sec") or 60))


def _summarize_rows(
    selected: list[dict[str, Any]],
    *,
    start_ts: int,
    end_ts: int,
) -> dict[str, Any]:
    covered_buckets = {
        (int(row.get("bucket_start") or 0), max(1, int(row.get("bucket_sec") or 60)))
        for row in selected
    }
    covered_sec = sum(bucket_sec for _bucket_start, bucket_sec in covered_buckets)
    window_sec = max(1, int(end_ts) - int(start_ts))
    coverage = min(1.0, covered_sec / window_sec)
    buy = sum(float(row.get("trade_buy_usd") or 0) for row in selected)
    sell = sum(float(row.get("trade_sell_usd") or 0) for row in selected)
    gross = buy + sell
    cvd = buy - sell
    exchanges = sorted({str(row.get("exchange") or "") for row in selected if str(row.get("exchange") or "")})
    price_source_exchange = "binance" if "binance" in exchanges else exchanges[0] if exchanges else ""
    price_rows = [
        row for row in selected
        if str(row.get("exchange") or "") == price_source_exchange
        if float(row.get("price_open") or 0) > 0 and float(row.get("price_close") or 0) > 0
    ]
    price_open = float(price_rows[0].get("price_open") or 0) if price_rows else None
    price_close = float(price_rows[-1].get("price_close") or 0) if price_rows else None
    price_high = max((float(row.get("price_high") or 0) for row in price_rows), default=0) or None
    lows = [float(row.get("price_low") or 0) for row in price_rows if float(row.get("price_low") or 0) > 0]
    price_low = min(lows) if lows else None
    price_change_pct = (
        (price_close - price_open) / price_open * 100
        if price_open and price_close is not None
        else None
    )
    cvd_ratio_pct = cvd / gross * 100 if gross > 0 else None
    long_liquidation = sum(float(row.get("long_liquidation_usd") or 0) for row in selected)
    short_liquidation = sum(float(row.get("short_liquidation_usd") or 0) for row in selected)
    available = coverage >= MIN_WINDOW_COVERAGE and gross > 0 and price_open is not None and price_close is not None
    return {
        "available": available,
        "start_ts": int(start_ts),
        "end_ts": int(end_ts),
        "coverage_ratio": round(coverage, 4),
        "bucket_count": len(selected),
        "time_bucket_count": len(covered_buckets),
        "exchanges": exchanges,
        "price_source_exchange": price_source_exchange,
        "trade_buy_usd": round(buy, 2),
        "trade_sell_usd": round(sell, 2),
        "gross_trade_usd": round(gross, 2),
        "cvd_usd": round(cvd, 2),
        "cvd_ratio_pct": round(cvd_ratio_pct, 4) if cvd_ratio_pct is not None else None,
        "price_open": price_open,
        "price_high": price_high,
        "price_low": price_low,
        "price_close": price_close,
        "price_change_pct": round(price_change_pct, 6) if price_change_pct is not None else None,
        "long_liquidation_usd": round(long_liquidation, 2),
        "short_liquidation_usd": round(short_liquidation, 2),
        "trade_count": sum(int(row.get("trade_count") or 0) for row in selected),
    }


def _aggregate_window(
    rows: list[dict[str, Any]],
    ends: list[int],
    *,
    start_ts: int,
    end_ts: int,
) -> dict[str, Any]:
    left = bisect_right(ends, int(start_ts))
    right = bisect_right(ends, int(end_ts))
    return _summarize_rows(rows[left:right], start_ts=start_ts, end_ts=end_ts)


def _five_minute_windows(rows: list[dict[str, Any]]) -> list[tuple[int, dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        end = _bucket_end(row)
        anchor = ((end + 299) // 300) * 300
        grouped[anchor].append(row)
    return [
        (anchor, _summarize_rows(grouped[anchor], start_ts=anchor - 300, end_ts=anchor))
        for anchor in sorted(grouped)
    ]


def _window_direction(window: dict[str, Any]) -> str:
    if not window.get("available"):
        return "neutral"
    cvd_ratio = float(window.get("cvd_ratio_pct") or 0)
    price_change = float(window.get("price_change_pct") or 0)
    if cvd_ratio >= 1.0 and price_change >= -0.25:
        return "long"
    if cvd_ratio <= -1.0 and price_change <= 0.25:
        return "short"
    return "neutral"


def _surge_from_windows(current: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    available = bool(current.get("available") and previous.get("available"))
    current_ratio = _number(current.get("cvd_ratio_pct"))
    previous_ratio = _number(previous.get("cvd_ratio_pct"))
    current_gross = float(current.get("gross_trade_usd") or 0)
    previous_gross = float(previous.get("gross_trade_usd") or 0)
    flow_acceleration = (
        current_ratio - previous_ratio
        if current_ratio is not None and previous_ratio is not None
        else None
    )
    volume_acceleration = (
        (current_gross / previous_gross - 1) * 100
        if previous_gross > 0
        else None
    )
    price_change = _number(current.get("price_change_pct")) or 0.0
    long_liq = float(current.get("long_liquidation_usd") or 0)
    short_liq = float(current.get("short_liquidation_usd") or 0)
    liq_total = long_liq + short_liq
    liq_bias = (short_liq - long_liq) / liq_total * 100 if liq_total > 0 else 0.0
    signed_impulse = (flow_acceleration or 0) + price_change * 2 + liq_bias * 0.05
    direction = "long" if signed_impulse > 0 else "short" if signed_impulse < 0 else "neutral"
    score = min(100.0, (
        abs(flow_acceleration or 0) * 0.9
        + min(100.0, abs(volume_acceleration or 0)) * 0.20
        + min(5.0, abs(price_change)) * 4
        + min(10.0, liq_total / max(current_gross, 1) * 100) * 1.5
    )) if available else 0.0
    triggered = bool(
        available
        and direction != "neutral"
        and score >= 35
        and abs(flow_acceleration or 0) >= 5
        and abs(current_ratio or 0) >= 2
    )
    return {
        "available": available,
        "triggered": triggered,
        "direction": direction,
        "score": round(score, 2),
        "flow_acceleration_pp": round(flow_acceleration, 4) if flow_acceleration is not None else None,
        "volume_acceleration_pct": round(volume_acceleration, 4) if volume_acceleration is not None else None,
        "price_change_pct": round(price_change, 6),
        "liquidation_bias_pct": round(liq_bias, 4),
        "current": current,
        "previous": previous,
        "method": "比较最近两个封闭 5 分钟窗口的主动成交差占比、成交额速度、价格和清算偏向。",
    }


def _surge_at(rows: list[dict[str, Any]], ends: list[int], anchor: int) -> dict[str, Any]:
    current = _aggregate_window(rows, ends, start_ts=anchor - 300, end_ts=anchor)
    previous = _aggregate_window(rows, ends, start_ts=anchor - 600, end_ts=anchor - 300)
    return _surge_from_windows(current, previous)


def _ambush(
    windows: dict[str, dict[str, Any]],
    surge: dict[str, Any],
) -> dict[str, Any]:
    short_window = windows["5m"]
    long_window = windows["15m"]
    ratio_5m = _number(short_window.get("cvd_ratio_pct"))
    ratio_15m = _number(long_window.get("cvd_ratio_pct"))
    price_15m = abs(_number(long_window.get("price_change_pct")) or 0)
    aligned = bool(
        ratio_5m is not None
        and ratio_15m is not None
        and ratio_5m * ratio_15m > 0
    )
    direction = "long" if aligned and ratio_15m > 0 else "short" if aligned else "neutral"
    flow_score = min(60.0, (abs(ratio_5m or 0) + abs(ratio_15m or 0)) * 1.2)
    compression_score = max(0.0, min(20.0, (2.0 - price_15m) / 2.0 * 20.0))
    acceleration = _number(surge.get("volume_acceleration_pct")) or 0
    activity_score = max(0.0, min(20.0, acceleration * 0.2))
    available = bool(short_window.get("available") and long_window.get("available"))
    score = flow_score + compression_score + activity_score if available else 0.0
    triggered = bool(
        available
        and aligned
        and not surge.get("triggered")
        and price_15m <= 2.0
        and abs(ratio_5m or 0) >= 3
        and abs(ratio_15m or 0) >= 3
        and score >= 45
    )
    return {
        "available": available,
        "triggered": triggered,
        "direction": direction,
        "score": round(min(100.0, score), 2),
        "price_compression_pct": round(price_15m, 6),
        "cvd_ratio_5m_pct": round(ratio_5m, 4) if ratio_5m is not None else None,
        "cvd_ratio_15m_pct": round(ratio_15m, 4) if ratio_15m is not None else None,
        "method": "5m 与 15m 主动成交差同向、价格仍压缩且尚未触发 Surge 时列为短周期潜伏候选。",
    }


def _rank(value: float | None, samples: list[float], *, method: str) -> dict[str, Any]:
    valid = sorted((sample for sample in samples if math.isfinite(sample)), reverse=True)
    if value is None or len(valid) < 2:
        return {
            "available": False,
            "sample_size": len(valid),
            "reason": "至少需要 2 个同口径样本",
            "method": method,
        }
    rank = 1 + sum(1 for sample in valid if sample > value)
    percentile = 100.0 * sum(1 for sample in valid if sample <= value) / len(valid)
    return {
        "available": True,
        "value": round(value, 4),
        "rank": rank,
        "sample_size": len(valid),
        "percentile": round(percentile, 1),
        "method": method,
    }


def _historical_strengths(
    five_minute_windows: list[tuple[int, dict[str, Any]]],
    anchor: int,
) -> list[float]:
    strengths: list[float] = []
    for window_anchor, window in five_minute_windows:
        if window_anchor < anchor - 86_400 or window_anchor > anchor:
            continue
        ratio = _number(window.get("cvd_ratio_pct"))
        if window.get("available") and ratio is not None:
            strengths.append(abs(ratio))
    return strengths


def _event_history_samples(
    rows: list[dict[str, Any]],
    ends: list[int],
    *,
    anchor: int,
    window_sec: int,
    metric: str,
) -> list[float]:
    samples: list[float] = []
    first_anchor = max(window_sec * 2, anchor - 86_400 + window_sec)
    sample_anchor = first_anchor - (first_anchor % window_sec)
    while sample_anchor <= anchor:
        current = _aggregate_window(
            rows,
            ends,
            start_ts=sample_anchor - window_sec,
            end_ts=sample_anchor,
        )
        if current.get("available"):
            value: float | None = None
            if metric == "price":
                value = abs(_number(current.get("price_change_pct")) or 0)
            elif metric == "volume":
                previous = _aggregate_window(
                    rows,
                    ends,
                    start_ts=sample_anchor - window_sec * 2,
                    end_ts=sample_anchor - window_sec,
                )
                current_gross = float(current.get("gross_trade_usd") or 0)
                previous_gross = float(previous.get("gross_trade_usd") or 0)
                if previous.get("available") and previous_gross > 0:
                    value = abs((current_gross / previous_gross - 1) * 100)
            elif metric == "perp_flow":
                value = abs(_number(current.get("cvd_ratio_pct")) or 0)
            elif metric == "liquidation":
                gross = float(current.get("gross_trade_usd") or 0)
                liquidations = (
                    float(current.get("long_liquidation_usd") or 0)
                    + float(current.get("short_liquidation_usd") or 0)
                )
                if gross > 0:
                    value = liquidations / gross * 100
            if value is not None and math.isfinite(value):
                samples.append(value)
        sample_anchor += window_sec
    return samples


def _build_anomaly_events(
    grouped: dict[str, list[dict[str, Any]]],
    *,
    anchor: int,
    limit: int = 120,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for symbol, rows in grouped.items():
        ends = [_bucket_end(row) for row in rows]
        for window_key, window_sec in ANOMALY_EVENT_WINDOWS:
            current = _aggregate_window(
                rows,
                ends,
                start_ts=anchor - window_sec,
                end_ts=anchor,
            )
            previous = _aggregate_window(
                rows,
                ends,
                start_ts=anchor - window_sec * 2,
                end_ts=anchor - window_sec,
            )
            if not current.get("available"):
                continue

            price_change = _number(current.get("price_change_pct"))
            if price_change is not None and abs(price_change) >= ANOMALY_PRICE_THRESHOLDS[window_key]:
                candidates.append({
                    "event_type": "price_up" if price_change > 0 else "price_down",
                    "label": "价格暴涨" if price_change > 0 else "价格暴跌",
                    "metric": "price",
                    "direction": "long" if price_change > 0 else "short",
                    "value": round(price_change, 6),
                    "value_usd": None,
                    "change_pct": round(price_change, 6),
                    "strength": abs(price_change),
                    "absolute_value": abs(price_change),
                    "threshold": ANOMALY_PRICE_THRESHOLDS[window_key],
                    "symbol": symbol,
                    "coin": symbol[:-4],
                    "window": window_key,
                    "window_sec": window_sec,
                    "observed_at": _iso_seconds(anchor),
                })

            current_gross = float(current.get("gross_trade_usd") or 0)
            previous_gross = float(previous.get("gross_trade_usd") or 0)
            volume_change = (
                (current_gross / previous_gross - 1) * 100
                if previous.get("available") and previous_gross > 0
                else None
            )
            if volume_change is not None and volume_change >= ANOMALY_VOLUME_THRESHOLDS[window_key]:
                price_direction = "long" if (price_change or 0) >= 0 else "short"
                candidates.append({
                    "event_type": "volume_spike",
                    "label": "成交量爆发",
                    "metric": "volume",
                    "direction": price_direction,
                    "value": round(current_gross, 2),
                    "value_usd": round(current_gross, 2),
                    "change_pct": round(volume_change, 4),
                    "strength": abs(volume_change),
                    "absolute_value": abs(current_gross),
                    "threshold": ANOMALY_VOLUME_THRESHOLDS[window_key],
                    "symbol": symbol,
                    "coin": symbol[:-4],
                    "window": window_key,
                    "window_sec": window_sec,
                    "observed_at": _iso_seconds(anchor),
                })

            cvd = float(current.get("cvd_usd") or 0)
            cvd_ratio = _number(current.get("cvd_ratio_pct"))
            if (
                cvd_ratio is not None
                and abs(cvd_ratio) >= ANOMALY_FLOW_THRESHOLDS[window_key]
                and abs(cvd) >= 1_000
            ):
                candidates.append({
                    "event_type": "perp_inflow" if cvd > 0 else "perp_outflow",
                    "label": "合约净流入" if cvd > 0 else "合约净流出",
                    "metric": "perp_flow",
                    "direction": "long" if cvd > 0 else "short",
                    "value": round(cvd, 2),
                    "value_usd": round(cvd, 2),
                    "change_pct": round(cvd_ratio, 4),
                    "strength": abs(cvd_ratio),
                    "absolute_value": abs(cvd),
                    "threshold": ANOMALY_FLOW_THRESHOLDS[window_key],
                    "symbol": symbol,
                    "coin": symbol[:-4],
                    "window": window_key,
                    "window_sec": window_sec,
                    "observed_at": _iso_seconds(anchor),
                })

            long_liquidation = float(current.get("long_liquidation_usd") or 0)
            short_liquidation = float(current.get("short_liquidation_usd") or 0)
            liquidation_total = long_liquidation + short_liquidation
            liquidation_ratio = liquidation_total / current_gross * 100 if current_gross > 0 else 0
            if liquidation_total >= 50_000 and liquidation_ratio >= 0.25:
                long_dominant = long_liquidation >= short_liquidation
                candidates.append({
                    "event_type": "long_liquidation" if long_dominant else "short_liquidation",
                    "label": "多头爆仓" if long_dominant else "空头爆仓",
                    "metric": "liquidation",
                    "direction": "short" if long_dominant else "long",
                    "value": round(liquidation_total, 2),
                    "value_usd": round(liquidation_total, 2),
                    "change_pct": round(liquidation_ratio, 4),
                    "strength": liquidation_ratio,
                    "absolute_value": liquidation_total,
                    "threshold": 0.25,
                    "symbol": symbol,
                    "coin": symbol[:-4],
                    "window": window_key,
                    "window_sec": window_sec,
                    "observed_at": _iso_seconds(anchor),
                })

        symbol_events = [event for event in candidates if event["symbol"] == symbol]
        for event in symbol_events:
            samples = _event_history_samples(
                rows,
                ends,
                anchor=anchor,
                window_sec=int(event["window_sec"]),
                metric=str(event["metric"]),
            )
            event["rankings"] = {
                "self": _rank(
                    float(event["strength"]),
                    samples,
                    method="当前同口径异常强度在该币种近 24h 封闭窗口样本中的经验排名",
                )
            }

    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for event in candidates:
        groups[(str(event["metric"]), str(event["window"]))].append(event)
    for events in groups.values():
        strength_samples = [
            float((event.get("rankings", {}).get("self", {}) or {}).get("percentile") or event["strength"])
            for event in events
        ]
        absolute_samples = [float(event["absolute_value"]) for event in events]
        for event in events:
            self_rank = event.get("rankings", {}).get("self", {}) or {}
            market_strength = float(self_rank.get("percentile") or event["strength"])
            event["rankings"]["market_strength"] = _rank(
                market_strength,
                strength_samples,
                method="同时间窗、同事件类型按各币历史极端分位进行全场排名",
            )
            event["rankings"]["market_absolute"] = _rank(
                float(event["absolute_value"]),
                absolute_samples,
                method="同时间窗、同事件类型按绝对金额或绝对变动进行全场排名",
            )
            event["id"] = f"{event['symbol']}:{event['window']}:{event['event_type']}:{anchor}"
            event.pop("strength", None)
            event.pop("absolute_value", None)
            event.pop("threshold", None)

    candidates.sort(
        key=lambda event: (
            int(((event.get("rankings") or {}).get("market_strength") or {}).get("rank") or 9_999),
            -abs(float(event.get("change_pct") or 0)),
            str(event.get("symbol") or ""),
        )
    )
    return candidates[:max(1, min(300, int(limit or 120)))]


def build_open_interest_anomaly_events(
    snapshot_rows: list[dict[str, Any]],
    *,
    now_ts: int,
    limit: int = 80,
) -> list[dict[str, Any]]:
    """Build truthful OI surge/dump events from persisted cross-window snapshots."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in snapshot_rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").upper()
        observed_at = int(row.get("observed_at") or 0)
        oi_usd = _number(row.get("oi_usd"))
        if symbol.endswith("USDT") and observed_at > 0 and oi_usd is not None and oi_usd > 0:
            grouped[symbol].append({"observed_at": observed_at, "oi_usd": oi_usd})
    for rows in grouped.values():
        rows.sort(key=lambda item: int(item["observed_at"]))

    window_specs = (("15m", 900, 1.0), ("1h", 3_600, 2.0))
    candidates: list[dict[str, Any]] = []
    for symbol, rows in grouped.items():
        times = [int(row["observed_at"]) for row in rows]
        latest = rows[-1]
        latest_at = int(latest["observed_at"])
        latest_oi = float(latest["oi_usd"])
        if int(now_ts) - latest_at > 7_200:
            continue
        for window_key, window_sec, threshold in window_specs:
            baseline_index = bisect_right(times, latest_at - window_sec) - 1
            if baseline_index < 0:
                continue
            baseline_oi = float(rows[baseline_index]["oi_usd"])
            if baseline_oi <= 0:
                continue
            change_usd = latest_oi - baseline_oi
            change_pct = change_usd / baseline_oi * 100
            if abs(change_pct) < threshold or abs(change_usd) < 1_000:
                continue
            samples: list[float] = []
            for index, point in enumerate(rows):
                point_at = int(point["observed_at"])
                if point_at < latest_at - 86_400:
                    continue
                previous_index = bisect_right(times, point_at - window_sec, 0, index) - 1
                if previous_index < 0:
                    continue
                previous_oi = float(rows[previous_index]["oi_usd"])
                if previous_oi > 0:
                    samples.append(abs((float(point["oi_usd"]) - previous_oi) / previous_oi * 100))
            rising = change_usd > 0
            candidates.append({
                "id": f"{symbol}:{window_key}:oi_{'up' if rising else 'down'}:{latest_at}",
                "symbol": symbol,
                "coin": symbol[:-4],
                "observed_at": _iso_seconds(latest_at),
                "window": window_key,
                "window_sec": window_sec,
                "event_type": "oi_up" if rising else "oi_down",
                "label": "OI 暴涨" if rising else "OI 暴跌",
                "metric": "oi",
                "direction": "long" if rising else "short",
                "value": round(change_usd, 2),
                "value_usd": round(change_usd, 2),
                "change_pct": round(change_pct, 4),
                "_strength": abs(change_pct),
                "_absolute": abs(change_usd),
                "rankings": {
                    "self": _rank(
                        abs(change_pct),
                        samples,
                        method="当前 OI 变动率在该币种近 24h 同窗口历史样本中的经验排名",
                    )
                },
            })

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in candidates:
        groups[str(event["window"])].append(event)
    for events in groups.values():
        strength_samples = [
            float((event["rankings"]["self"] or {}).get("percentile") or event["_strength"])
            for event in events
        ]
        absolute_samples = [float(event["_absolute"]) for event in events]
        for event in events:
            strength = float((event["rankings"]["self"] or {}).get("percentile") or event["_strength"])
            event["rankings"]["market_strength"] = _rank(
                strength,
                strength_samples,
                method="同一封闭窗口按各币 OI 历史极端分位进行全场排名",
            )
            event["rankings"]["market_absolute"] = _rank(
                float(event["_absolute"]),
                absolute_samples,
                method="同一封闭窗口按 OI 绝对变化金额进行全场排名",
            )
            event.pop("_strength", None)
            event.pop("_absolute", None)
    candidates.sort(
        key=lambda event: (
            int(((event.get("rankings") or {}).get("market_strength") or {}).get("rank") or 9_999),
            -abs(float(event.get("change_pct") or 0)),
        )
    )
    return candidates[:max(1, min(200, int(limit or 80)))]


def _anomaly_count_24h(
    five_minute_windows: list[tuple[int, dict[str, Any]]],
    anchor: int,
) -> dict[str, Any]:
    long_count = 0
    short_count = 0
    latest_at = ""
    previous: dict[str, Any] | None = None
    for window_anchor, current in five_minute_windows:
        if window_anchor < anchor - 86_400 or window_anchor > anchor:
            continue
        if previous is not None:
            surge = _surge_from_windows(current, previous)
            if surge.get("triggered"):
                direction = str(surge.get("direction") or "neutral")
                if direction == "long":
                    long_count += 1
                elif direction == "short":
                    short_count += 1
                latest_at = _iso_seconds(window_anchor)
        previous = current
    return {
        "count": long_count + short_count,
        "long_count": long_count,
        "short_count": short_count,
        "latest_at": latest_at,
        "window_sec": 86_400,
        "method": "统计近 24 小时相邻封闭 5 分钟窗口中达到 Surge 规则阈值的次数。",
    }


def _resonance(windows: dict[str, dict[str, Any]]) -> dict[str, Any]:
    directions = {
        key: _window_direction(windows.get(key, {}))
        for key, _seconds in MOMENTUM_WINDOWS
    }
    active = [direction for direction in directions.values() if direction != "neutral"]
    counts = Counter(active)
    direction = counts.most_common(1)[0][0] if counts else "neutral"
    active_count = counts.get(direction, 0) if direction != "neutral" else 0
    return {
        "available": any(windows.get(key, {}).get("available") for key, _seconds in MOMENTUM_WINDOWS),
        "direction": direction if active_count >= 2 else "neutral",
        "active_count": active_count,
        "window_count": len(windows),
        "windows": [
            {
                "key": key,
                "active": directions[key] == direction and active_count >= 2,
                "direction": directions[key],
                "coverage_ratio": windows.get(key, {}).get("coverage_ratio"),
            }
            for key, _seconds in MOMENTUM_WINDOWS
        ],
        "method": "15m、30m、1h、4h、1d 封闭窗口的 CVD 方向需获得价格非反向确认；至少两个周期同向才算方向共振。",
    }


def _lifecycle(
    rows: list[dict[str, Any]],
    ends: list[int],
    *,
    anchor: int,
    surge: dict[str, Any],
    ambush: dict[str, Any],
) -> dict[str, Any]:
    current_active = bool(surge.get("triggered") or ambush.get("triggered"))
    current_score = max(float(surge.get("score") or 0), float(ambush.get("score") or 0))
    previous: tuple[int, float] | None = None
    for offset in range(300, 3_601, 300):
        candidate_anchor = anchor - offset
        candidate = _surge_at(rows, ends, candidate_anchor)
        if candidate.get("triggered"):
            previous = (candidate_anchor, float(candidate.get("score") or 0))
            break
    if current_active and previous is None:
        state, basis = "new", "过去 1 小时没有同方向 Surge，当前为新异常"
    elif current_active and previous is not None:
        gap = anchor - previous[0]
        if gap > 1_800:
            state, basis = "restarted", f"沉寂 {max(1, round(gap / 60))} 分钟后再次触发"
        elif current_score >= previous[1] + 8:
            state, basis = "enhancing", f"异常分较上次提高 {current_score - previous[1]:.1f}"
        else:
            state, basis = "continuing", "最近 30 分钟异常方向持续"
    elif previous is not None and anchor - previous[0] <= 1_800:
        state, basis = "cooling", "此前异常仍在 30 分钟观察期内，但当前已低于触发阈值"
    elif previous is not None:
        state, basis = "expired", "最近异常已超过 30 分钟且没有重新触发"
    else:
        state, basis = "inactive", "当前与过去 1 小时均未达到异常阈值"
    labels = {
        "new": "NEW", "enhancing": "增强", "continuing": "持续",
        "cooling": "降温", "restarted": "重启", "expired": "失效", "inactive": "未触发",
    }
    return {"state": state, "label": labels[state], "basis": basis, "observed_at": _iso_seconds(anchor)}


def _close_at(
    rows: list[dict[str, Any]],
    ends: list[int],
    target: int,
    *,
    exchange: str,
) -> float | None:
    index = bisect_left(ends, target)
    while index < len(rows) and ends[index] <= target + 60:
        row = rows[index]
        if str(row.get("exchange") or "") == exchange:
            value = _number(row.get("price_close"))
            return value if value is not None and value > 0 else None
        index += 1
    return None


def _backtest(
    grouped: dict[str, list[dict[str, Any]]],
    windows_by_symbol: dict[str, list[tuple[int, dict[str, Any]]]] | None = None,
) -> dict[str, Any]:
    outcomes: dict[str, list[float]] = {key: [] for key, _seconds in BACKTEST_HORIZONS}
    signal_count = 0
    for symbol, rows in grouped.items():
        ends = [_bucket_end(row) for row in rows]
        if len(ends) < 12:
            continue
        windows = (windows_by_symbol or {}).get(symbol) or _five_minute_windows(rows)
        for index in range(1, len(windows)):
            anchor, current = windows[index]
            _previous_anchor, previous = windows[index - 1]
            surge = _surge_from_windows(current, previous)
            if not surge.get("triggered"):
                continue
            entry_price = _number(surge.get("current", {}).get("price_close"))
            price_exchange = str(surge.get("current", {}).get("price_source_exchange") or "")
            direction = str(surge.get("direction") or "neutral")
            if (
                entry_price is None
                or entry_price <= 0
                or not price_exchange
                or direction not in {"long", "short"}
            ):
                continue
            signal_count += 1
            direction_sign = 1 if direction == "long" else -1
            for key, seconds in BACKTEST_HORIZONS:
                exit_price = _close_at(rows, ends, anchor + seconds, exchange=price_exchange)
                if exit_price is None:
                    continue
                raw_return = (exit_price - entry_price) / entry_price * 100
                outcomes[key].append(raw_return * direction_sign)
    horizons: dict[str, Any] = {}
    for key, _seconds in BACKTEST_HORIZONS:
        samples = outcomes[key]
        sample_size = len(samples)
        horizons[key] = {
            "status": "ready" if sample_size >= MIN_BACKTEST_SAMPLES else "insufficient",
            "sample_size": sample_size,
            "hit_rate_pct": round(sum(1 for value in samples if value > 0) / sample_size * 100, 2) if samples else None,
            "average_directional_return_pct": round(mean(samples), 6) if samples else None,
            "median_directional_return_pct": round(median(samples), 6) if samples else None,
        }
    return {
        "status": "ready" if all(item["status"] == "ready" for item in horizons.values()) else "insufficient",
        "signal_count": signal_count,
        "minimum_sample_size": MIN_BACKTEST_SAMPLES,
        "horizons": horizons,
        "method": "仅用信号时点之后的封闭分钟价格计算 5m/15m/1h 方向收益；不使用未来数据参与信号。",
        "disclaimer": "历史统计不含手续费、滑点和成交约束，不构成投资建议。",
    }


def build_realtime_intelligence(
    feature_rows: list[dict[str, Any]],
    *,
    now_ts: int,
    limit: int = 10,
    include_backtest: bool = False,
) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in feature_rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").upper()
        if not symbol.endswith("USDT"):
            continue
        grouped[symbol].append(row)
    for rows in grouped.values():
        rows.sort(key=lambda row: (_bucket_end(row), str(row.get("exchange") or "")))
    anchor = max((_bucket_end(row) for rows in grouped.values() for row in rows), default=0)
    if anchor <= 0:
        return {
            "schema_version": REALTIME_INTELLIGENCE_SCHEMA_VERSION,
            "generated_at": _iso_seconds(int(now_ts)),
            "observed_at": "",
            "data_status": "unavailable",
            "items": [],
            "anomaly_events": [],
            "boards": [],
            "backtest": _backtest({}) if include_backtest else None,
        }

    items: list[dict[str, Any]] = []
    five_minute_windows_by_symbol: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for symbol, rows in grouped.items():
        ends = [_bucket_end(row) for row in rows]
        five_minute_windows = _five_minute_windows(rows)
        five_minute_windows_by_symbol[symbol] = five_minute_windows
        windows = {
            key: _aggregate_window(rows, ends, start_ts=anchor - seconds, end_ts=anchor)
            for key, seconds in INTELLIGENCE_WINDOWS
        }
        surge = _surge_at(rows, ends, anchor)
        ambush = _ambush(windows, surge)
        resonance = _resonance(windows)
        anomaly_24h = _anomaly_count_24h(five_minute_windows, anchor)
        current_strength = abs(_number(windows["5m"].get("cvd_ratio_pct")) or 0)
        items.append({
            "symbol": symbol,
            "coin": symbol[:-4],
            "observed_at": _iso_seconds(anchor),
            "data_status": "ready" if windows["5m"].get("available") else "partial",
            "windows": windows,
            "surge": surge,
            "ambush": ambush,
            "resonance": resonance,
            "anomaly_24h": anomaly_24h,
            "lifecycle": _lifecycle(rows, ends, anchor=anchor, surge=surge, ambush=ambush),
            "rankings": {
                "self": _rank(
                    current_strength,
                    _historical_strengths(five_minute_windows, anchor),
                    method="当前 5m CVD 占比绝对值在该币近 24h 非重叠 5m 窗口中的经验分位",
                ),
            },
        })

    strength_samples = [float(item["surge"].get("score") or 0) for item in items]
    absolute_samples = [float(item["windows"]["5m"].get("gross_trade_usd") or 0) for item in items]
    for item in items:
        item["rankings"]["market_strength"] = _rank(
            float(item["surge"].get("score") or 0),
            strength_samples,
            method="同一封闭 5m 时点的 Surge 规则分横截面排名",
        )
        item["rankings"]["market_absolute"] = _rank(
            float(item["windows"]["5m"].get("gross_trade_usd") or 0),
            absolute_samples,
            method="同一封闭 5m 时点的合约成交额绝对规模排名",
        )

    safe_limit = max(1, min(30, int(limit or 10)))
    anomaly_events = _build_anomaly_events(
        grouped,
        anchor=anchor,
        limit=safe_limit,
    )
    surge_items = sorted(
        (item for item in items if item["surge"].get("triggered")),
        key=lambda item: (float(item["surge"].get("score") or 0), item["symbol"]),
        reverse=True,
    )
    ambush_items = sorted(
        (item for item in items if item["ambush"].get("triggered")),
        key=lambda item: (float(item["ambush"].get("score") or 0), item["symbol"]),
        reverse=True,
    )
    total_items = sorted(
        (item for item in items if int((item.get("anomaly_24h") or {}).get("count") or 0) > 0),
        key=lambda item: (
            int((item.get("anomaly_24h") or {}).get("count") or 0),
            float(item["surge"].get("score") or 0),
            item["symbol"],
        ),
        reverse=True,
    )
    boards = [
        {
            "key": "surge", "title": "Surge 加速", "count": len(surge_items),
            "description": "最近两个 5m 封闭窗口的资金速度与价格/清算确认。",
            "items": surge_items[:safe_limit],
        },
        {
            "key": "ambush", "title": "短周期潜伏", "count": len(ambush_items),
            "description": "5m/15m 资金同向但价格尚未充分移动的候选。",
            "items": ambush_items[:safe_limit],
        },
        {
            "key": "total", "title": "24h 异动总榜", "count": len(total_items),
            "description": "近 24 小时相邻封闭 5m 窗口达到 Surge 规则阈值的累计次数。",
            "items": total_items[:safe_limit],
        },
    ]
    return {
        "schema_version": REALTIME_INTELLIGENCE_SCHEMA_VERSION,
        "generated_at": _iso_seconds(int(now_ts)),
        "observed_at": _iso_seconds(anchor),
        "data_status": "ready" if any(item["data_status"] == "ready" for item in items) else "partial",
        "coverage": {
            "symbols": len(items),
            "surge": len(surge_items),
            "ambush": len(ambush_items),
            "total": len(total_items),
            "anomaly_events": len(anomaly_events),
        },
        "methodology": {
            "surge": "封闭分钟特征计算，不使用当前未完成分钟。",
            "ambush": "规则候选，不等于价格预测或交易建议。",
            "resonance": "只在 15m/30m/1h/4h/1d 中至少两个封闭窗口同方向且价格未反向时确认。",
            "total": "24h 总榜统计相邻封闭 5m 窗口达到 Surge 规则阈值的次数。",
            "ranking": "自身、横截面强度和绝对成交规模使用不同口径。",
        },
        "items": sorted(
            items,
            key=lambda item: (float(item["surge"].get("score") or 0), item["symbol"]),
            reverse=True,
        )[:safe_limit],
        "anomaly_events": anomaly_events,
        "boards": boards,
        "backtest": _backtest(grouped, five_minute_windows_by_symbol) if include_backtest else None,
    }


def build_realtime_intelligence_radar_boards(
    payload: dict[str, Any],
    *,
    limit: int = 8,
) -> list[dict[str, Any]]:
    safe_limit = max(1, min(20, int(limit or 8)))

    def board_item(item: dict[str, Any], metric: str) -> dict[str, Any]:
        analysis = item.get(metric) if isinstance(item.get(metric), dict) else {}
        direction = str(analysis.get("direction") or "neutral")
        score = float(analysis.get("score") or 0)
        rank = item.get("rankings", {}).get("market_strength", {})
        return {
            "symbol": str(item.get("symbol") or ""),
            "coin": str(item.get("coin") or ""),
            "value": round(score if direction == "long" else -score, 2),
            "unit": "score",
            "magnitude_usd": None,
            "strength_percentile": rank.get("percentile"),
            "updated_at": str(item.get("observed_at") or ""),
            "status": "fresh",
            "quality": "websocket_closed_bucket",
            "direction": direction,
            "lifecycle": item.get("lifecycle"),
            "resonance": item.get("resonance"),
        }

    def build_board(metric: str, key: str, title: str, positive_title: str, negative_title: str) -> dict[str, Any]:
        items = [
            item for item in payload.get("items", [])
            if isinstance(item, dict) and bool((item.get(metric) or {}).get("triggered"))
        ]
        positives = sorted(
            (item for item in items if str((item.get(metric) or {}).get("direction")) == "long"),
            key=lambda item: float((item.get(metric) or {}).get("score") or 0),
            reverse=True,
        )[:safe_limit]
        negatives = sorted(
            (item for item in items if str((item.get(metric) or {}).get("direction")) == "short"),
            key=lambda item: float((item.get(metric) or {}).get("score") or 0),
            reverse=True,
        )[:safe_limit]
        return {
            "key": key,
            "title": title,
            "metric": f"{metric}_score",
            "unit": "score",
            "available": bool(items),
            "coverage": int((payload.get("coverage") or {}).get("symbols") or 0),
            "positive": {"title": positive_title, "items": [board_item(item, metric) for item in positives]},
            "negative": {"title": negative_title, "items": [board_item(item, metric) for item in negatives]},
            "reason": "" if items else "当前没有达到规则阈值的候选",
        }

    total_items = sorted(
        (
            item for item in payload.get("items", [])
            if isinstance(item, dict) and int((item.get("anomaly_24h") or {}).get("count") or 0) > 0
        ),
        key=lambda item: int((item.get("anomaly_24h") or {}).get("count") or 0),
        reverse=True,
    )[:safe_limit]
    total_board = {
        "key": "realtime_total",
        "title": "24h 异动总榜",
        "metric": "anomaly_count_24h",
        "unit": "count",
        "available": bool(total_items),
        "coverage": int((payload.get("coverage") or {}).get("symbols") or 0),
        "positive": {
            "title": "异动次数",
            "items": [
                {
                    "symbol": str(item.get("symbol") or ""),
                    "coin": str(item.get("coin") or ""),
                    "value": int((item.get("anomaly_24h") or {}).get("count") or 0),
                    "unit": "count",
                    "updated_at": str((item.get("anomaly_24h") or {}).get("latest_at") or ""),
                    "status": "fresh",
                    "quality": "websocket_closed_bucket",
                    "resonance": item.get("resonance"),
                }
                for item in total_items
            ],
        },
        "negative": {"title": "", "items": []},
        "reason": "" if total_items else "近 24 小时没有达到规则阈值的异动",
    }

    return [
        build_board("surge", "realtime_surge", "Surge 加速", "多头加速", "空头加速"),
        build_board("ambush", "realtime_ambush", "短周期潜伏", "多头潜伏", "空头潜伏"),
        total_board,
    ]


__all__ = [
    "BACKTEST_HORIZONS",
    "INTELLIGENCE_WINDOWS",
    "MOMENTUM_WINDOWS",
    "MIN_BACKTEST_SAMPLES",
    "REALTIME_INTELLIGENCE_SCHEMA_VERSION",
    "build_realtime_intelligence",
    "build_realtime_intelligence_radar_boards",
]
